"""
olympusrepo/core/import_git.py
Git repository importer — brings existing git history into OlympusRepo.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import os
import re
import shutil
import subprocess
import tempfile
import json
from . import db, objects, repo as repo_mod


# Absolute path to git. The web server process inherits its own PATH and
# may not see the user's shell-configured locations (e.g. under WSL or
# systemd), so bare "git" can ENOENT at runtime. shutil.which() honours
# the process PATH; we fall back to /usr/bin/git for the common case.
GIT_BIN = shutil.which("git") or os.environ.get("OLYMPUSREPO_GIT_BIN") or "/usr/bin/git"


# ---------------------------------------------------------------------------
# Subprocess hardening
# ---------------------------------------------------------------------------
# Git has a long history of protocol/submodule-related footguns. These args
# are prepended to every git invocation to shut them down:
#   * protocol.allow=never              — default-deny all transports
#   * protocol.https.allow=always       — re-enable https only
#   * protocol.http.allow=always        — plain http (for internal mirrors)
#   * protocol.file.allow=user          — file:// only when operator opts in
#   * protocol.ext.allow=never          — kills ext:: (historical RCE)
#
# GIT_TERMINAL_PROMPT=0 stops git from blocking on credential prompts that
# cannot be answered by a web request.
GIT_SAFE_ARGS = [
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "protocol.http.allow=always",
    "-c", "protocol.file.allow=user",
    "-c", "protocol.ext.allow=never",
]
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
GIT_TIMEOUT_SECONDS = int(os.environ.get("OLYMPUSREPO_GIT_TIMEOUT", "300"))


def _git(args: list, cwd: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        [GIT_BIN, *GIT_SAFE_ARGS, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env=GIT_ENV,
    )
    return result.stdout.strip()


def _get_commits(git_dir: str, branch: str = None) -> list[dict]:
    """
    Get all commits in chronological order.
    Returns list of {hash, author, email, timestamp, message}
    """
    fmt = "%H|%an|%ae|%at|%s"
    branch_arg = branch or "HEAD"
    output = _git(
        ["log", "--reverse", f"--format={fmt}", branch_arg],
        git_dir
    )
    commits = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) == 5:
            commits.append({
                "hash":      parts[0],
                "author":    parts[1],
                "email":     parts[2],
                "timestamp": int(parts[3]),
                "message":   parts[4],
            })
    return commits


def _get_files_at_commit(git_dir: str, commit_hash: str) -> dict[str, bytes]:
    """
    Get all file contents at a specific commit.
    Returns {relative_path: content_bytes}
    """
    # Get list of files at this commit
    output = _git(
        ["ls-tree", "-r", "--name-only", commit_hash],
        git_dir
    )
    files = {}
    # Patterns to skip
    skip_patterns = ['.venv/', 'venv/', '__pycache__/', 
                     'node_modules/', '.git/', 'objects/',
                     '.egg-info/', '.pytest_cache/']
    
    for path in output.splitlines():
        if not path.strip():
            continue
        # Skip ignored paths
        if any(path.startswith(p) or ('/' + p) in path 
               for p in skip_patterns):
            continue
        if path.endswith(('.pyc', '.pyo')):
            continue
        # Skip binary-unfriendly paths
        try:
            result = subprocess.run(
                [GIT_BIN, *GIT_SAFE_ARGS, "show", f"{commit_hash}:{path}"],
                cwd=git_dir,
                capture_output=True,
                check=True,
                timeout=GIT_TIMEOUT_SECONDS,
                env=GIT_ENV,
            )
            files[path] = result.stdout
        except subprocess.CalledProcessError:
            pass
    return files


def import_git_repo(
    conn,
    git_source: str,
    repo_name: str,
    user_id: int,
    objects_dir: str,
    branch: str = None,
    progress_cb=None
) -> dict:
    """
    Import a git repository into OlympusRepo.

    Args:
        conn:        DB connection
        git_source:  path to existing git repo OR remote URL
        repo_name:   name for the new OlympusRepo repository
        user_id:     user to attribute commits to
        objects_dir: blob store path
        branch:      git branch to import (default: default branch)
        progress_cb: optional callback(current, total, message)

    Returns:
        {"repo_id": int, "commits_imported": int, "files_imported": int}
    """
    tmp_dir = None
    git_dir = git_source

    # Defense in depth: refuse any URL-ish source that begins with "-".
    # The caller should already reject these, but older versions of git
    # mis-parsed leading-dash arguments as options (CVE-2017-1000117 era),
    # and protecting here keeps the library safe in isolation.
    if git_source.startswith("-"):
        raise ValueError("git source must not start with '-'")

    # If it's a URL, clone it into a temp dir (argv invocation, no shell).
    # The "--" terminator makes any future git version treat the URL as a
    # positional, even if it somehow contains flag-like characters.
    if git_source.startswith(("http://", "https://", "git://", "git@")):
        tmp_dir = tempfile.mkdtemp(prefix="olympus_import_")
        print(f"  Cloning {git_source}...")
        try:
            subprocess.run(
                [GIT_BIN, *GIT_SAFE_ARGS, "clone", "--quiet",
                 "--no-tags", "--no-recurse-submodules",
                 "--", git_source, tmp_dir],
                check=True,
                timeout=GIT_TIMEOUT_SECONDS,
                env=GIT_ENV,
                capture_output=True,
                text=True,
            )
        except Exception:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        git_dir = tmp_dir
    else:
        # Treat as a local path. Must be an existing directory — this stops
        # silently succeeding on typos that happen to parse as file names.
        if not os.path.isdir(git_source):
            raise ValueError(
                f"Local git path not found or not a directory: {git_source}"
            )

    # Validate branch before it hits argv. Git refs are fairly permissive,
    # but we whitelist the common-safe set to make sure no branch name can
    # be interpreted as a flag (e.g. "-u ..." or "--output=...").
    if branch is not None:
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9/_.\-]{0,199}$', branch):
            raise ValueError(f"Invalid branch name: {branch!r}")

    try:
        # Detect default branch if not specified
        if not branch:
            try:
                branch = _git(
                    ["rev-parse", "--abbrev-ref", "HEAD"],
                    git_dir
                )
            except Exception:
                branch = "main"

        # Create the OlympusRepo repository
        r = repo_mod.create_repo(
            conn, repo_name, user_id, visibility="public"
        )
        repo_id = r["repo_id"]

        # Get all commits
        commits = _get_commits(git_dir, branch)
        total = len(commits)
        print(f"  Found {total} commit(s) on branch '{branch}'")

        imported = 0
        total_files = 0

        for i, c in enumerate(commits):
            if progress_cb:
                progress_cb(i + 1, total, c["message"][:50])
            else:
                print(f"  [{i+1}/{total}] {c['hash'][:8]} {c['message'][:60]}")

            # Get files at this commit
            files_at_commit = _get_files_at_commit(git_dir, c["hash"])
            if not files_at_commit:
                continue

            file_list = list(files_at_commit.items())
            total_files += len(file_list)

            # Import commit preserving original message and author
            # Use commit_files() which handles the full DB transaction
            result = repo_mod.commit_files(
                conn, repo_id, user_id,
                f"{c['message']}\n\n[imported from git:{c['hash'][:8]}]"
                if imported == 0 else c["message"],
                file_list,
                objects_dir
            )
            if result:
                imported += 1

        return {
            "repo_id":          repo_id,
            "repo_name":        repo_name,
            "commits_imported": imported,
            "files_imported":   total_files,
            "branch":           branch,
        }

    finally:
        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)