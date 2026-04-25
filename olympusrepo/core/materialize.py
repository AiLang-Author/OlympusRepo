"""
olympusrepo/core/materialize.py
Tree materialization for Olympus commits.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License

Walks commit ancestry applying changesets to reconstruct the full file
tree at any commit. Needed anywhere we need to emit a complete tree
(fast-import for push, gateway sync, file browsing at arbitrary revs).

Algorithm:
  Walk parents backward until we hit an anchor — either a root commit
  or an imported commit (which has a full snapshot from import_git.py).
  Then apply changesets forward from anchor to target.

Correctness notes:
  * On merge commits we follow parent_hashes[0] (the mainline parent).
    This matches git's "first-parent" convention and is what reviewers
    intuitively expect when asking "what files are in this commit".
  * For materializing a merge commit's actual content (not just first-
    parent lineage), the changesets on the merge commit itself contain
    the resolution of any conflicts. Those get applied last, which
    overwrites the first-parent tree state correctly.
"""

from typing import Optional


def materialize_tree(
    conn,
    repo_id: int,
    commit_hash: str,
) -> dict[str, tuple[str, str]]:
    """
    Return {path: (blob_hash, file_mode)} for the full tree at commit_hash.

    Empty dict if the commit has no content (e.g. truly empty initial
    commit). Raises ValueError if commit_hash isn't in the repo.
    """
    chain = _ancestor_chain_to_anchor(conn, repo_id, commit_hash)
    if not chain:
        raise ValueError(
            f"commit {commit_hash!r} not found in repo {repo_id}"
        )

    tree: dict[str, tuple[str, str]] = {}
    # chain[0] is the target, chain[-1] is the anchor. Apply ancestor-
    # first so later changes overwrite earlier ones.
    for sha in reversed(chain):
        _apply_changeset(conn, sha, tree)
    return tree


def _ancestor_chain_to_anchor(
    conn, repo_id: int, commit_hash: str,
) -> list[str]:
    """
    Return [target, parent, grandparent, ...] stopping at the first
    imported commit or a root. Follows first-parent on merges.
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
            # Dangling parent (shallow import). Treat as anchor.
            break
        is_imported, parents = row
        chain.append(sha)
        if is_imported:
            # Imports store full snapshots — stop here.
            break
        sha = (parents or [None])[0]
    return chain


def _apply_changeset(
    conn, commit_hash: str, tree: dict[str, tuple[str, str]],
) -> None:
    """Mutate `tree` by applying the changeset rows for this commit."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT path, change_type, blob_after, old_path, file_mode
            FROM repo_changesets
            WHERE commit_hash = %s
        """, (commit_hash,))
        rows = cur.fetchall()

    for path, ctype, blob_after, old_path, file_mode in rows:
        mode = file_mode or '100644'
        if ctype in ('add', 'modify'):
            if blob_after is None:
                continue  # malformed row; skip defensively
            tree[path] = (blob_after, mode)
        elif ctype == 'delete':
            tree.pop(path, None)
        elif ctype == 'rename':
            # Rename moves the old entry's content to new path. If
            # blob_after is set, content also changed during the rename.
            old_entry = tree.pop(old_path, None) if old_path else None
            if blob_after is not None:
                tree[path] = (blob_after, mode)
            elif old_entry is not None:
                tree[path] = old_entry


def tree_summary(
    conn, repo_id: int, commit_hash: str,
) -> dict:
    """Counts + total size for a commit's tree. Useful for UI / quotas."""
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
