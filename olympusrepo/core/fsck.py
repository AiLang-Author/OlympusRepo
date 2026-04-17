# =========================================================================
# olympusrepo/core/fsck.py
# Repository integrity checker and repair utility.
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
# MIT License
# =========================================================================

import os
from . import db, objects as obj_store


def check(conn, repo_id: int, objects_dir: str,
          fix: bool = False) -> dict:
    """
    Check repository integrity. Returns a results dict with:
      - missing_blobs:   [(commit_hash, path, blob_hash), ...]
      - orphaned_blobs:  [blob_hash, ...]
      - null_blob_after: [(commit_hash, path), ...]
      - fixed:           count of repairs made (if fix=True)

    Does not modify anything unless fix=True.
    """
    results = {
        "missing_blobs":   [],
        "orphaned_blobs":  [],
        "null_blob_after": [],
        "fixed":           0,
    }

    # ── 1. Find changesets referencing blobs that don't exist on disk ──
    rows = db.query(conn, """
        SELECT cs.commit_hash, cs.path, cs.blob_after
          FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
         WHERE c.repo_id = %s
           AND cs.blob_after IS NOT NULL
           AND cs.change_type IN ('add', 'modify')
    """, (repo_id,))

    for row in rows:
        if not obj_store.exists(row["blob_after"], objects_dir):
            results["missing_blobs"].append((
                row["commit_hash"], row["path"], row["blob_after"]
            ))

    # ── 2. Find changesets with NULL blob_after on non-delete entries ──
    null_rows = db.query(conn, """
        SELECT cs.commit_hash, cs.path
          FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
         WHERE c.repo_id = %s
           AND cs.change_type IN ('add', 'modify')
           AND cs.blob_after IS NULL
    """, (repo_id,))

    for row in null_rows:
        results["null_blob_after"].append((
            row["commit_hash"], row["path"]
        ))

    # ── 3. Find orphaned blobs (on disk but not referenced by any commit) ──
    # Collect all referenced hashes across ALL repos (blobs are shared)
    ref_rows = db.query(conn, """
        SELECT DISTINCT blob_after AS h FROM repo_changesets
         WHERE blob_after IS NOT NULL
        UNION
        SELECT DISTINCT tree_hash   AS h FROM repo_commits
         WHERE tree_hash IS NOT NULL
    """, ())
    referenced = {r["h"] for r in ref_rows}

    for blob_hash in obj_store.list_objects(objects_dir):
        if blob_hash not in referenced:
            results["orphaned_blobs"].append(blob_hash)

    return results


def prune(conn, objects_dir: str, dry_run: bool = False) -> int:
    """
    Delete orphaned blobs from the object store.
    Returns count of objects deleted (or that would be deleted on dry_run).
    """
    ref_rows = db.query(conn, """
        SELECT DISTINCT blob_after AS h FROM repo_changesets
         WHERE blob_after IS NOT NULL
        UNION
        SELECT DISTINCT tree_hash AS h FROM repo_commits
         WHERE tree_hash IS NOT NULL
    """, ())
    referenced = {r["h"] for r in ref_rows}

    if dry_run:
        count = sum(
            1 for h in obj_store.list_objects(objects_dir)
            if h not in referenced
        )
        return count

    return obj_store.gc_unreferenced(referenced, objects_dir)

