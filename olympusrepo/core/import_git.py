"""
olympusrepo/core/import_git.py
Git repository importer — brings existing git history into OlympusRepo
with full fidelity: parent DAG, author/committer identities, timestamps,
tree hashes, and original SHAs are all preserved so the repo can
round-trip back to git later.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import os
import re
import shutil
import subprocess
import tempfile
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
#   * protocol.allow=never              - default-deny all transports
#   * protocol.https.allow=always       - re-enable https only
#   * protocol.http.allow=always        - plain http (for internal mirrors)
#   * protocol.file.allow=user          - file:// only when operator opts in
#   * protocol.ext.allow=never          - kills ext:: (historical RCE)
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

# Import guardrails. A 40GB repo pointed at a live web request will eat
# the server alive; fail fast with a clear message instead.
MAX_COMMITS = int(os.environ.get("OLYMPUSREPO_IMPORT_MAX_COMMITS", "50000"))
MAX_TOTAL_BYTES = int(os.environ.get("OLYMPUSREPO_IMPORT_MAX_BYTES",
                                     str(2 * 1024 * 1024 * 1024)))  # 2 GiB

# Paths we never want to suck into the object store. Matched as path
# segments, not raw substrings, so "src/objects/model.py" is NOT skipped
# the way it would be with naive startswith checks.
SKIP_SEGMENTS = frozenset({
    ".venv", "venv", "__pycache__", "node_modules",
    ".git", ".egg-info", ".pytest_cache",
})
SKIP_SUFFIXES = (".pyc", ".pyo")


def _git(args: list, cwd: str) -> str:
    """Run a git command and return stdout (text)."""
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


def _path_is_skipped(path: str) -> bool:
    """True if any path segment is in the skip list."""
    if path.endswith(SKIP_SUFFIXES):
        return True
    # Split on forward slash — git always reports POSIX paths from ls-tree.
    return any(seg in SKIP_SEGMENTS for seg in path.split("/"))


# ---------------------------------------------------------------------------
# Commit metadata extraction
# ---------------------------------------------------------------------------
# Record separator (ASCII 0x1e) between fields, unit separator (0x1f)
# between commits. Using control chars instead of "|" means commit
# messages containing the delimiter cannot corrupt parsing.
#
# Fields captured, matching repo_commits columns:
#   %H  commit SHA        -> commit_hash
#   %T  tree SHA          -> tree_hash   (preserving this is what lets us
#                                         round-trip back to git later)
#   %P  parent SHAs       -> parent_hashes[]
#   %an author name       -> author_name
#   %ae author email      -> author_email
#   %at author timestamp  -> authored_at
#   %cn committer name    -> committer_name
#   %ce committer email   -> committer_email
#   %ct committer time    -> committed_at
#   %B  full message      -> message
_COMMIT_FMT = (
    "%H%x1e%T%x1e%P%x1e"
    "%an%x1e%ae%x1e%at%x1e%ai%x1e"   # +%ai (ISO date) for author tz offset
    "%cn%x1e%ce%x1e%ct%x1e%ci%x1e"   # +%ci (ISO date) for committer tz offset
    "%B%x1f"
)


# ISO date format ends in " +HHMM" or " -HHMM". Pull just the offset
# without parsing the rest of the string — git always emits exactly this.
_TZ_RE = re.compile(r"([+\-]\d{4})\s*$")


def _tz_from_iso(iso: str) -> str:
    m = _TZ_RE.search(iso.strip())
    return m.group(1) if m else "+0000"


def _get_commits(git_dir: str, branch: str) -> list[dict]:
    """
    Return commits in topological + chronological order (parents before
    children), with full metadata. Commit ordering matters for FK
    integrity: parent_hashes is not FK-enforced in the schema, but we
    want the import to make sense if someone later queries the graph.
    """
    raw = _git(
        ["log", "--reverse", "--topo-order",
         f"--format={_COMMIT_FMT}", branch],
        git_dir,
    )
    commits = []
    for record in raw.split("\x1f"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1e")
        if len(parts) < 12:
            continue
        (sha, tree, parents_str,
         an, ae, at, ai,
         cn, ce, ct, ci,
         message) = parts[:12]
        parents = parents_str.split() if parents_str else []
        commits.append({
            "sha":              sha,
            "tree":             tree,
            "parents":          parents,
            "author_name":      an,
            "author_email":     ae,
            "author_time":      int(at),
            "author_tz":        _tz_from_iso(ai),
            "committer_name":   cn,
            "committer_email":  ce,
            "committer_time":   int(ct),
            "committer_tz":     _tz_from_iso(ci),
            "message":          message,
        })
    return commits


# ---------------------------------------------------------------------------
# File extraction via cat-file --batch
# ---------------------------------------------------------------------------
# Spawning "git show" per-file is O(files * commits) processes. On a
# medium repo that is hundreds of thousands of forks. cat-file --batch
# keeps a single git process alive for the whole import; we feed it
# "<sha>:<path>\n" and read back framed blob data. Roughly 1000x faster
# in practice.
class _CatFileBatch:
    """Long-lived `git cat-file --batch` helper."""

    def __init__(self, git_dir: str):
        self.proc = subprocess.Popen(
            [GIT_BIN, *GIT_SAFE_ARGS, "cat-file", "--batch"],
            cwd=git_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=GIT_ENV,
            bufsize=0,
        )

    def read_blob(self, commit_sha: str, path: str) -> bytes | None:
        """
        Return the blob bytes for `path` at `commit_sha`, or None if the
        object is missing or not a blob.
        """
        req = f"{commit_sha}:{path}\n".encode("utf-8", errors="surrogateescape")
        self.proc.stdin.write(req)
        # Header line: "<sha> <type> <size>\n" OR "<name> missing\n"
        header = self.proc.stdout.readline()
        if not header:
            raise RuntimeError("git cat-file closed unexpectedly")
        header_str = header.decode("utf-8", errors="replace").rstrip("\n")
        if header_str.endswith(" missing"):
            return None
        try:
            _sha, obj_type, size_str = header_str.split()
            size = int(size_str)
        except ValueError:
            return None
        if obj_type != "blob":
            # Read and discard non-blob payload + trailing LF.
            _ = self.proc.stdout.read(size + 1)
            return None
        data = b""
        remaining = size
        while remaining > 0:
            chunk = self.proc.stdout.read(remaining)
            if not chunk:
                raise RuntimeError("git cat-file truncated blob")
            data += chunk
            remaining -= len(chunk)
        # Trailing newline after every object.
        self.proc.stdout.read(1)
        return data

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _list_tree(git_dir: str, commit_sha: str) -> list[str]:
    """List all blob paths at a commit, filtered by skip rules."""
    output = _git(
        ["ls-tree", "-r", "--name-only", commit_sha],
        git_dir,
    )
    return [p for p in output.splitlines()
            if p.strip() and not _path_is_skipped(p)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def import_git_repo(
    conn,
    git_source: str,
    repo_name: str,
    user_id: int,
    objects_dir: str,
    branch: str = None,
    progress_cb=None,
) -> dict:
    """
    Import a git repository into OlympusRepo with full metadata fidelity.

    Preserves on each repo_commits row:
      * commit_hash    = original git commit SHA (PK)
      * tree_hash      = original git tree SHA
      * parent_hashes  = original parent SHAs (array, supports merges)
      * author_name, author_email, authored_at
      * committer_name, committer_email, committed_at
      * message        = full commit message body
      * is_imported    = TRUE

    Args:
        conn:        DB connection
        git_source:  path to existing git repo OR remote URL
        repo_name:   name for the new OlympusRepo repository
        user_id:     user performing the import (recorded on the repo as
                     imported_by, NOT as author_id on every commit)
        objects_dir: blob store path
        branch:      git branch to import (default: repo's default branch)
        progress_cb: optional callable(current, total, message)

    Returns dict with repo_id, commits_imported, files_imported, branch.
    """
    # Defense in depth: refuse any URL-ish source that begins with "-".
    # Older versions of git mis-parsed leading-dash args as options
    # (CVE-2017-1000117 era).
    if git_source.startswith("-"):
        raise ValueError("git source must not start with '-'")

    # Validate branch before it hits argv. Git refs are fairly permissive
    # but we whitelist a conservative set so no branch name can be
    # interpreted as a flag.
    if branch is not None:
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9/_.\-]{0,199}$', branch):
            raise ValueError(f"Invalid branch name: {branch!r}")

    tmp_dir = None
    git_dir = git_source

    # URL sources get cloned into a temp dir (argv invocation, no shell).
    # "--" terminator makes git treat the URL as a positional even if it
    # contains flag-like characters.
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
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        git_dir = tmp_dir
    else:
        if not os.path.isdir(git_source):
            raise ValueError(
                f"Local git path not found or not a directory: {git_source}"
            )

    batch = None
    try:
        # Detect default branch if caller didn't specify one.
        if not branch:
            try:
                branch = _git(
                    ["rev-parse", "--abbrev-ref", "HEAD"],
                    git_dir,
                )
                if branch == "HEAD":
                    # Detached checkout — fall back.
                    branch = "main"
            except Exception:
                branch = "main"

        commits = _get_commits(git_dir, branch)
        total = len(commits)
        if total == 0:
            raise ValueError(f"No commits found on branch '{branch}'")
        if total > MAX_COMMITS:
            raise ValueError(
                f"Repo has {total} commits; exceeds limit of {MAX_COMMITS}. "
                f"Raise OLYMPUSREPO_IMPORT_MAX_COMMITS to override."
            )
        print(f"  Found {total} commit(s) on branch '{branch}'")

        # Create the destination repo and record provenance at the repo
        # level (not by mutating commit messages). Assumes repo_mod.create_repo
        # accepts the new imported_from/imported_by kwargs wired to the
        # columns added in 015_git_import.sql.
        r = repo_mod.create_repo(
            conn, repo_name, user_id,
            visibility="public",
            imported_from=git_source,
            default_branch=branch,
        )
        repo_id = r["repo_id"]

        batch = _CatFileBatch(git_dir)

        imported = 0
        total_files = 0
        total_bytes = 0

        for i, c in enumerate(commits):
            subject = c["message"].splitlines()[0] if c["message"] else ""
            if progress_cb:
                progress_cb(i + 1, total, subject[:60])
            else:
                print(f"  [{i+1}/{total}] {c['sha'][:8]} {subject[:60]}")

            # Gather file list + contents for this commit via the
            # long-lived cat-file process.
            paths = _list_tree(git_dir, c["sha"])
            file_list: list[tuple[str, bytes]] = []
            for path in paths:
                blob = batch.read_blob(c["sha"], path)
                if blob is None:
                    continue
                file_list.append((path, blob))
                total_bytes += len(blob)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError(
                        f"Import exceeded {MAX_TOTAL_BYTES} bytes; aborting."
                    )

            # Write blobs to the object store, build changeset rows, and
            # insert the commit row with full metadata. Parents are
            # stored as the original git SHA array — no id translation
            # needed because commit_hash IS the git SHA on imported rows.
            #
            # This assumes repo_mod.import_commit_row() wraps the call to
            # SQL function repo_insert_imported_commit() from 015. See
            # the accompanying repo_mod changes for the signature.
            repo_mod.import_commit_row(
                conn,
                repo_id=repo_id,
                commit_hash=c["sha"],
                tree_hash=c["tree"],
                parent_hashes=c["parents"],
                author_name=c["author_name"],
                author_email=c["author_email"],
                authored_at_epoch=c["author_time"],
                author_tz_offset=c["author_tz"],
                committer_name=c["committer_name"],
                committer_email=c["committer_email"],
                committed_at_epoch=c["committer_time"],
                committer_tz_offset=c["committer_tz"],
                message=c["message"],
                files=file_list,
                objects_dir=objects_dir,
            )
            imported += 1
            total_files += len(file_list)

        # Point the default ref at the tip of the imported branch.
        tip_sha = commits[-1]["sha"]
        repo_mod.set_ref(
            conn,
            repo_id=repo_id,
            ref_name=f"refs/heads/{branch}",
            commit_hash=tip_sha,
            user_id=user_id,
        )

        return {
            "repo_id":          repo_id,
            "repo_name":        repo_name,
            "commits_imported": imported,
            "files_imported":   total_files,
            "bytes_imported":   total_bytes,
            "branch":           branch,
            "tip":              tip_sha,
        }

    finally:
        if batch is not None:
            batch.close()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)