
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License
#
# Local state lives in .olympusrepo/ at the repo root:
#   .olympusrepo/
#     config.json         - repo URL, user, branch, repo_id
#     HEAD                - current branch name
#     index.json          - staged files: {path: {hash, mtime, size}}
#     index_committed.json- last committed state (used by diff/status)
#     olympusignore       - patterns to ignore (optional, in repo root)
#     pending/            - offline commits waiting to sync
#     cache/              - cached object content for offline work

import fnmatch
import json
import os
import time
from pathlib import Path

from . import objects

REPO_DIR    = ".olympusrepo"
CONFIG_FILE = "config.json"
HEAD_FILE   = "HEAD"
INDEX_FILE  = "index.json"
INDEX_COMMITTED_FILE = "index_committed.json"
IGNORE_FILE = ".olympusignore"
PENDING_DIR = "pending"
CACHE_DIR   = "cache"

# Default files/directories to always ignore
DEFAULT_IGNORE_PATTERNS = [
    ".olympusrepo",
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "node_modules",
    ".DS_Store",
    "Thumbs.db",
    ".venv",
    "venv",
    "*.egg-info",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "objects",
]


def find_repo_root(start_path: str = ".") -> str | None:
    """Walk up from start_path looking for .olympusrepo/ directory."""
    path = os.path.abspath(start_path)
    while True:
        if os.path.isdir(os.path.join(path, REPO_DIR)):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def init_local(repo_root: str, config: dict):
    """Initialize .olympusrepo/ directory with config."""
    repo_dir = os.path.join(repo_root, REPO_DIR)
    os.makedirs(repo_dir, exist_ok=True)
    os.makedirs(os.path.join(repo_dir, PENDING_DIR), exist_ok=True)
    os.makedirs(os.path.join(repo_dir, CACHE_DIR), exist_ok=True)

    # Write config atomically (tmp+rename) so a mid-init crash doesn't
    # leave a half-written config.json that breaks future CLI calls.
    save_config(repo_root, config)

    head_path = os.path.join(repo_dir, HEAD_FILE)
    with open(head_path, "w") as f:
        f.write(config.get("default_branch", "main"))

    # Both indexes start empty
    index_path = os.path.join(repo_dir, INDEX_FILE)
    with open(index_path, "w") as f:
        json.dump({}, f)

    committed_path = os.path.join(repo_dir, INDEX_COMMITTED_FILE)
    with open(committed_path, "w") as f:
        json.dump({}, f)


def load_config(repo_root: str) -> dict:
    """Load .olympusrepo/config.json."""
    path = os.path.join(repo_root, REPO_DIR, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_config(repo_root: str, config: dict):
    """Save .olympusrepo/config.json atomically via tmp+rename.

    A mid-write crash (Ctrl+C, OOM, power loss) previously left config.json
    truncated, which bricked every subsequent CLI invocation in that repo.
    Using tmp+rename ensures the file is either the old content or the new
    content — never partial — because rename is atomic on POSIX filesystems.
    """
    path = os.path.join(repo_root, REPO_DIR, CONFIG_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)


def get_current_branch(repo_root: str) -> str:
    """Read current branch from HEAD file."""
    path = os.path.join(repo_root, REPO_DIR, HEAD_FILE)
    if not os.path.exists(path):
        return "main"
    with open(path, "r") as f:
        return f.read().strip()


def set_current_branch(repo_root: str, branch: str):
    """Write current branch to HEAD file."""
    path = os.path.join(repo_root, REPO_DIR, HEAD_FILE)
    with open(path, "w") as f:
        f.write(branch)


# =========================================================================
# IGNORE PATTERNS
# =========================================================================

def load_ignore_patterns(repo_root: str) -> list[str]:
    """Load ignore patterns: defaults + .olympusignore."""
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    ignore_path = os.path.join(repo_root, IGNORE_FILE)
    if os.path.exists(ignore_path):
        with open(ignore_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def _should_ignore(name: str, patterns: list[str]) -> bool:
    """Check if a file/dir name matches any ignore pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


# =========================================================================
# INDEX — staged state
# =========================================================================

def load_index(repo_root: str) -> dict:
    """
    Load staged index.
    { "path/to/file": {"hash": "abc...", "mtime": 123456, "size": 789} }
    """
    path = os.path.join(repo_root, REPO_DIR, INDEX_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_index(repo_root: str, index: dict):
    """Save staged index atomically."""
    path = os.path.join(repo_root, REPO_DIR, INDEX_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, path)


def load_committed_index(repo_root: str) -> dict:
    """
    Load the last-committed index.
    This is what diff and status compare against.
    Falls back to index.json if index_committed.json doesn't exist
    (backward compatibility with repos created before this change).
    """
    path = os.path.join(repo_root, REPO_DIR, INDEX_COMMITTED_FILE)
    if not os.path.exists(path):
        # Backward compat: treat current index as committed baseline
        return load_index(repo_root)
    with open(path, "r") as f:
        return json.load(f)


def save_committed_index(repo_root: str, index: dict):
    """
    Snapshot the committed state. Called by commit() after success
    and by clone() after writing files to disk.
    """
    path = os.path.join(repo_root, REPO_DIR, INDEX_COMMITTED_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, path)


def update_index_entry(repo_root: str, filepath: str, obj_hash: str):
    """Update a single file's entry in the staged index."""
    index = load_index(repo_root)
    full_path = os.path.join(repo_root, filepath)
    stat = os.stat(full_path)
    index[filepath] = {
        "hash":  obj_hash,
        "mtime": stat.st_mtime,
        "size":  stat.st_size,
    }
    save_index(repo_root, index)


# =========================================================================
# SCANNING
# =========================================================================

def scan_working_tree(repo_root: str) -> list[str]:
    """Recursively list all trackable files. Returns relative paths."""
    patterns = load_ignore_patterns(repo_root)
    files = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if not _should_ignore(d, patterns)]
        for fname in filenames:
            if _should_ignore(fname, patterns):
                continue
            full_path = os.path.join(dirpath, fname)
            rel_path  = os.path.relpath(full_path, repo_root)
            files.append(rel_path)
    return sorted(files)


# =========================================================================
# CHANGE DETECTION
# =========================================================================

def detect_changes(repo_root: str, objects_dir: str = None) -> dict:
    """
    Compare working tree against the COMMITTED index (not staged).

    This is what `olympusrepo diff` and `olympusrepo commit` use.
    Shows what has changed since the last commit, regardless of staging.

    Returns:
      "modified": [(path, old_hash, new_hash), ...]
      "added":    [(path, new_hash), ...]
      "deleted":  [(path, old_hash), ...]
    """
    if objects_dir is None:
        objects_dir = os.path.join(repo_root, REPO_DIR, CACHE_DIR)

    committed = load_committed_index(repo_root)
    working_files  = set(scan_working_tree(repo_root))
    committed_files = set(committed.keys())

    changes = {"modified": [], "added": [], "deleted": []}

    for filepath in working_files:
        full_path = os.path.join(repo_root, filepath)
        if filepath in committed_files:
            entry = committed[filepath]
            stat  = os.stat(full_path)
            # Fast path: mtime+size match and mtime is non-zero
            if (entry.get("mtime") and
                    stat.st_mtime == entry.get("mtime") and
                    stat.st_size  == entry.get("size")):
                continue
            # Slow path: hash compare
            new_hash = objects.hash_file(full_path)
            if new_hash != entry.get("hash"):
                changes["modified"].append((filepath, entry["hash"], new_hash))
        else:
            new_hash = objects.hash_file(full_path)
            changes["added"].append((filepath, new_hash))

    for filepath in committed_files:
        if filepath not in working_files:
            changes["deleted"].append((filepath, committed[filepath]["hash"]))

    return changes


def detect_staged_changes(repo_root: str) -> dict:
    """
    Compare staged index against committed index.
    This is what `olympusrepo status` uses to show staged changes.

    Returns:
      "modified": [(path, old_hash, new_hash), ...]
      "added":    [(path, new_hash), ...]
      "deleted":  [(path, old_hash), ...]
    """
    staged    = load_index(repo_root)
    committed = load_committed_index(repo_root)

    changes = {"modified": [], "added": [], "deleted": []}

    for path, entry in staged.items():
        new_hash = entry["hash"] if isinstance(entry, dict) else entry
        if path not in committed:
            changes["added"].append((path, new_hash))
        else:
            old_hash = committed[path]["hash"] if isinstance(committed[path], dict) else committed[path]
            if new_hash != old_hash:
                changes["modified"].append((path, old_hash, new_hash))

    for path, entry in committed.items():
        if path not in staged:
            old_hash = entry["hash"] if isinstance(entry, dict) else entry
            changes["deleted"].append((path, old_hash))

    return changes


# =========================================================================
# PENDING (offline commits)
# =========================================================================

def save_pending_commit(repo_root: str, commit_data: dict):
    """Save a commit to the pending queue for later sync."""
    pending_dir = os.path.join(repo_root, REPO_DIR, PENDING_DIR)
    os.makedirs(pending_dir, exist_ok=True)
    ts   = int(time.time() * 1000)
    path = os.path.join(pending_dir, f"{ts}.json")
    with open(path, "w") as f:
        json.dump(commit_data, f, indent=2)


def list_pending_commits(repo_root: str) -> list[dict]:
    """List all pending commits in order."""
    pending_dir = os.path.join(repo_root, REPO_DIR, PENDING_DIR)
    if not os.path.isdir(pending_dir):
        return []
    commits = []
    for fname in sorted(os.listdir(pending_dir)):
        if fname.endswith(".json"):
            path = os.path.join(pending_dir, fname)
            with open(path, "r") as f:
                data = json.load(f)
                data["_pending_file"] = path
                commits.append(data)
    return commits


def clear_pending_commit(pending_path: str):
    """Remove a pending commit after successful sync."""
    if os.path.exists(pending_path):
        os.remove(pending_path)
