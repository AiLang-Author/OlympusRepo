"""
olympusrepo/core/import_git.py
Git repository importer — brings existing git history into OlympusRepo.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import os
import subprocess
import tempfile
import json
from . import db, objects, repo as repo_mod


def _git(args: list, cwd: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True
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
    for path in output.splitlines():
        if not path.strip():
            continue
        # Skip binary-unfriendly paths
        try:
            result = subprocess.run(
                ["git", "show", f"{commit_hash}:{path}"],
                cwd=git_dir,
                capture_output=True,
                check=True
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

    # If it's a URL, clone it first
    if git_source.startswith("http") or git_source.startswith("git@"):
        tmp_dir = tempfile.mkdtemp(prefix="olympus_import_")
        print(f"  Cloning {git_source}...")
        subprocess.run(
            ["git", "clone", "--quiet", git_source, tmp_dir],
            check=True
        )
        git_dir = tmp_dir

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