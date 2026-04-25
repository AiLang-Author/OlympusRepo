"""
olympusrepo/core/materialize.py
Tree materialization for OlympusRepo commits.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License

Reconstructs the full file tree at any commit by walking changesets.
Needed anywhere a complete tree must be emitted: git fast-import push,
git gateway sync, file browsing at arbitrary historical revisions.

Algorithm
---------
Walk parent_hashes[0] (first-parent) backward until we reach either:
  * a root commit (no parents), or
  * an imported commit (is_imported=TRUE) — imports store full-tree
    snapshots (every file as an 'add' row), so the walk can stop.

Then apply changesets forward from the anchor to the target commit.
Merge commit resolution: the changesets on the merge commit itself
contain the conflict resolution and are applied last, which correctly
overwrites the first-parent tree state.

Phase notes
-----------
Phase 2 (this file): file_mode hardcoded to '100644'. The column
  doesn't exist in repo_changesets until migration 017 (Phase 4).
Phase 4 upgrade: replace _apply_changeset with the version that
  SELECTs file_mode from repo_changesets after 017 runs. The
  public API (materialize_tree, tree_for_export) is unchanged.
"""

from typing import Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def materialize_tree(
    conn,
    repo_id: int,
    commit_hash: str,
) -> dict[str, tuple[str, str]]:
    """
    Return {path: (blob_hash, file_mode)} for every file in the tree at
    commit_hash.

    file_mode is one of git's standard mode strings:
      '100644'  regular file
      '100755'  executable (Phase 4+, always '100644' until then)
      '120000'  symlink    (Phase 4+)
      '160000'  gitlink/submodule (Phase 4+)

    Returns an empty dict for a genuinely empty commit tree.
    Raises ValueError if commit_hash is not found in this repo.
    """
    chain = _ancestor_chain_to_anchor(conn, repo_id, commit_hash)
    if not chain:
        raise ValueError(
            f"commit {commit_hash!r} not found in repo {repo_id}"
        )

    tree: dict[str, tuple[str, str]] = {}
    # chain[0] = target, chain[-1] = oldest anchor.
    # Apply oldest-first so later commits correctly overwrite earlier state.
    for sha in reversed(chain):
        _apply_changeset(conn, sha, tree)
    return tree


def tree_for_export(
    conn,
    repo_id: int,
    commit_hash: str,
) -> list[tuple[str, str, str]]:
    """
    Convenience wrapper for the fast-import / gateway export path.
    Returns [(path, blob_hash, file_mode), ...] sorted by path.
    Callers should not parse the mode — pass it straight to fast-import.
    """
    tree = materialize_tree(conn, repo_id, commit_hash)
    return sorted(
        (path, blob_hash, mode)
        for path, (blob_hash, mode) in tree.items()
    )


def tree_summary(conn, repo_id: int, commit_hash: str) -> dict:
    """
    File count + total stored size for a commit's tree.
    Useful for UI display and quota enforcement.
    Falls back gracefully if repo_objects rows are missing (loose objects
    not yet catalogued don't break the summary — they just show as 0 bytes).
    """
    tree = materialize_tree(conn, repo_id, commit_hash)
    if not tree:
        return {"files": 0, "bytes": 0}

    blob_hashes = list({blob for blob, _ in tree.values()})
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(size_bytes), 0)
            FROM repo_objects
            WHERE object_hash = ANY(%s)
        """, (blob_hashes,))
        total = cur.fetchone()[0]
    return {"files": len(tree), "bytes": int(total or 0)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ancestor_chain_to_anchor(
    conn, repo_id: int, commit_hash: str,
) -> list[str]:
    """
    Return [target, parent, grandparent, ...] stopping at the first
    imported commit or root (no parents). Follows parent_hashes[0]
    (first-parent convention) on merge commits.

    Returns an empty list if commit_hash is not found in the repo.
    """
    chain: list[str] = []
    visited: set[str] = set()
    sha: Optional[str] = commit_hash

    while sha and sha not in visited:
        visited.add(sha)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT is_imported, parent_hashes
                FROM repo_commits
                WHERE commit_hash = %s AND repo_id = %s
            """, (sha, repo_id))
            row = cur.fetchone()

        if not row:
            # Dangling parent — shallow import or data integrity issue.
            # Treat whatever we have so far as the anchor and stop.
            break

        is_imported, parents = row
        chain.append(sha)

        if is_imported:
            # Imported commits store full-tree snapshots (all 'add' rows).
            # No need to walk further back.
            break

        # First-parent walk; stops naturally at root (parents is None or []).
        sha = (parents or [None])[0]

    return chain


def _apply_changeset(
    conn,
    commit_hash: str,
    tree: dict[str, tuple[str, str]],
) -> None:
    """
    Mutate `tree` in-place by applying the repo_changesets rows for
    commit_hash.

    Phase 2 note: file_mode is not yet a column in repo_changesets
    (that arrives in migration 017, Phase 4). All files are emitted
    as '100644' (regular non-executable). When Phase 4 lands, replace
    this function with the version that SELECTs file_mode directly.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT path, change_type, blob_after, old_path
            FROM repo_changesets
            WHERE commit_hash = %s
        """, (commit_hash,))
        rows = cur.fetchall()

    for path, ctype, blob_after, old_path in rows:
        # Phase 2: hardcoded mode. Phase 4 will read this from the row.
        mode = '100644'

        if ctype in ('add', 'modify'):
            if blob_after is None:
                # Malformed row — skip defensively rather than poisoning
                # the tree with a None blob_hash.
                continue
            tree[path] = (blob_after, mode)

        elif ctype == 'delete':
            tree.pop(path, None)

        elif ctype == 'rename':
            # A rename may or may not include a content change.
            # Pop the old path first; if blob_after is set the content
            # changed during the rename, otherwise carry the old blob.
            old_entry = tree.pop(old_path, None) if old_path else None
            if blob_after is not None:
                tree[path] = (blob_after, mode)
            elif old_entry is not None:
                tree[path] = old_entry
            # If neither: old_path wasn't in the tree and no new blob
            # was provided — this shouldn't happen in well-formed data
            # but we skip rather than crash.