# In olympusrepo/core/export_git.py
#
# 1. Add this import near the top of the file, alongside the other
#    core imports:
#
#      from . import materialize as mat
#
# 2. Replace the entire _files_at_commit function with the version below.
#    The public signature is unchanged so push_to_git needs no edits.
# ==========================================================================


def _files_at_commit(conn, repo_id: int, commit_hash: str) -> list[tuple[str, str]]:
    """
    Return [(path, blob_hash), ...] sorted by path, representing the
    full file tree at commit_hash.

    Uses materialize.materialize_tree so both imported commits (full
    snapshots) and native promotion commits (delta changesets) are
    handled correctly. Without this, a native commit pushed to a git
    remote would be missing files that existed before the commit but
    weren't touched by it.

    file_mode is intentionally dropped here; the fast-import emitter in
    push_to_git still hard-codes '100644' for Phase 2. When Phase 4
    lands and migration 017 adds the file_mode column, switch the
    emitter to call tree_for_export() instead, which returns the mode
    per-file.
    """
    try:
        tree = mat.materialize_tree(conn, repo_id, commit_hash)
    except ValueError:
        # commit_hash not found in this repo — shouldn't happen in
        # normal push flow, but return empty rather than crashing so
        # the caller can surface a clean error.
        return []

    # Return (path, blob_hash) only; caller doesn't need mode yet.
    return sorted(
        (path, blob_hash)
        for path, (blob_hash, _mode) in tree.items()
    )