
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License
#
# Local state lives in .olympusrepo/ at the repo root:
#   .olympusrepo/
#     config.json        - repo URL, user, branch, repo_id
#     HEAD               - current branch name
#     index.json         - manifest of tracked files: {path: {hash, mtime, size}}
#     olympusignore      - patterns to ignore (optional, in repo root as .olympusignore)
#     pending/           - offline commits waiting to sync
#     cache/             - cached object content for offline work

import fnmatch
import json
import os
import time
from pathlib import Path

from . import objects

REPO_DIR = ".olympusrepo"
CONFIG_FILE = "config.json"
HEAD_FILE = "HEAD"
INDEX_FILE = "index.json"
IGNORE_FILE = ".olympusignore"
PENDING_DIR = "pending"
CACHE_DIR = "cache"

# Default files/directories to always ignore (exact name or glob pattern)
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

    # Write config
    config_path = os.path.join(repo_dir, CONFIG_FILE)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    # Write HEAD
    head_path = os.path.join(repo_dir, HEAD_FILE)
    with open(head_path, "w") as f:
        f.write(config.get("default_branch", "main"))

    # Write empty index
    index_path = os.path.join(repo_dir, INDEX_FILE)
    with open(index_path, "w") as f:
        json.dump({}, f)


def load_config(repo_root: str) -> dict:
    """Load .olympusrepo/config.json."""
    path = os.path.join(repo_root, REPO_DIR, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


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
    """
    Load ignore patterns: built-in defaults + any from .olympusignore at repo root.
    Patterns support glob-style wildcards via fnmatch.
    """
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
# INDEX — tracks which files are at which hash
# =========================================================================

def load_index(repo_root: str) -> dict:
    """
    Load index. Returns dict of:
    { "path/to/file": {"hash": "abc...", "mtime": 123456, "size": 789} }
    """
    path = os.path.join(repo_root, REPO_DIR, INDEX_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_index(repo_root: str, index: dict):
    """Save index to disk atomically."""
    path = os.path.join(repo_root, REPO_DIR, INDEX_FILE)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp_path, path)


def update_index_entry(repo_root: str, filepath: str, obj_hash: str):
    """Update a single file's entry in the index."""
    index = load_index(repo_root)
    full_path = os.path.join(repo_root, filepath)
    stat = os.stat(full_path)

    index[filepath] = {
        "hash": obj_hash,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }

    save_index(repo_root, index)


# =========================================================================
# SCANNING — detect changes in working tree
# =========================================================================

def scan_working_tree(repo_root: str) -> list[str]:
    """
    Recursively list all tracked/trackable files in the working tree.
    Returns relative paths from repo_root.
    """
    patterns = load_ignore_patterns(repo_root)
    files = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Filter out ignored directories (modifies in-place to prevent descent)
        dirnames[:] = [d for d in dirnames if not _should_ignore(d, patterns)]

        for fname in filenames:
            if _should_ignore(fname, patterns):
                continue
            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, repo_root)
            files.append(rel_path)

    return sorted(files)


def detect_changes(repo_root: str, objects_dir: str = None) -> dict:
    """
    Compare working tree against the index.

    Returns dict with keys:
      "modified": [(path, old_hash, new_hash), ...]
      "added":    [(path, new_hash), ...]
      "deleted":  [(path, old_hash), ...]
    """
    if objects_dir is None:
        objects_dir = os.path.join(repo_root, REPO_DIR, CACHE_DIR)

    index = load_index(repo_root)
    working_files = set(scan_working_tree(repo_root))
    indexed_files = set(index.keys())

    changes = {"modified": [], "added": [], "deleted": []}

    # Check for modified and added files
    for filepath in working_files:
        full_path = os.path.join(repo_root, filepath)

        if filepath in indexed_files:
            entry = index[filepath]
            stat = os.stat(full_path)

            # Fast path: if mtime and size unchanged, skip hashing
            if (stat.st_mtime == entry.get("mtime") and
                    stat.st_size == entry.get("size")):
                continue

            # Slow path: hash and compare
            new_hash = objects.hash_file(full_path)
            if new_hash != entry.get("hash"):
                changes["modified"].append((filepath, entry["hash"], new_hash))
        else:
            # New file
            new_hash = objects.hash_file(full_path)
            changes["added"].append((filepath, new_hash))

    # Check for deleted files
    for filepath in indexed_files:
        if filepath not in working_files:
            changes["deleted"].append((filepath, index[filepath]["hash"]))

    return changes


# =========================================================================
# PENDING (offline commits)
# =========================================================================

def save_pending_commit(repo_root: str, commit_data: dict):
    """Save a commit to the pending queue for later sync."""
    pending_dir = os.path.join(repo_root, REPO_DIR, PENDING_DIR)
    os.makedirs(pending_dir, exist_ok=True)

    # Use timestamp as filename for ordering
    ts = int(time.time() * 1000)
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
