"""
Additions to olympusrepo/core/repo.py for git import support.
Assumes the existing module already defines:
  * create_repo(conn, name, user_id, visibility=...) -> {"repo_id": int, ...}
  * commit_files(conn, repo_id, user_id, message, files, objects_dir)
  * set_ref(conn, repo_id, ref_name, commit_hash, user_id) -- or similar
  * objects.write_blob(objects_dir, content_bytes) -> blob_hash
  * db module conventions

If your create_repo signature doesn't yet accept imported_from/default_branch
keywords, extend it — it should write them directly to repo_repositories.
The columns exist after 015_git_import.sql runs.
"""

from datetime import datetime, timezone


def import_commit_row(
    conn,
    *,
    repo_id: int,
    commit_hash: str,
    tree_hash: str,
    parent_hashes: list[str],
    author_name: str,
    author_email: str,
    authored_at_epoch: int,
    committer_name: str,
    committer_email: str,
    committed_at_epoch: int,
    message: str,
    files: list[tuple[str, bytes]],
    objects_dir: str,
) -> dict:
    """
    Insert one git-imported commit with full fidelity.

    - Writes every file to the blob store (idempotent by content hash)
    - Inserts repo_changesets rows for each file as 'add' (imports treat
      each commit as a full snapshot; diffs are reconstructable later
      from parent tree comparison if/when needed)
    - Inserts repo_commits via repo_insert_imported_commit() SQL function,
      which sets is_imported = TRUE and lets Postgres assign rev

    Returns {"commit_hash": str, "rev": int, "files_written": int}.

    Transaction: this function does NOT manage its own transaction.
    The caller (import_git_repo's loop) should wrap the whole import in
    a single transaction so a mid-import failure rolls back cleanly
    instead of leaving a half-imported repo.
    """
    authored_at = datetime.fromtimestamp(authored_at_epoch, tz=timezone.utc)
    committed_at = datetime.fromtimestamp(committed_at_epoch, tz=timezone.utc)

    # 1. Store blobs. objects.write_blob should be idempotent on hash
    #    collisions so re-imports and shared files are cheap.
    from . import objects
    files_written = 0
    path_to_blob: dict[str, str] = {}
    for path, content in files:
        blob_hash = objects.write_blob(objects_dir, content)
        path_to_blob[path] = blob_hash
        files_written += 1

    with conn.cursor() as cur:
        # 2. Insert the commit row. The SQL function handles rev
        #    assignment and sets is_imported = TRUE.
        cur.execute(
            """
            SELECT repo_insert_imported_commit(
                %(repo_id)s,
                %(commit_hash)s,
                %(tree_hash)s,
                %(parent_hashes)s,
                %(author_name)s,
                %(author_email)s,
                %(authored_at)s,
                %(committer_name)s,
                %(committer_email)s,
                %(committed_at)s,
                %(message)s,
                NULL,  -- author_id: imported commits aren't tied to a local user
                NULL   -- committer_id: same
            ) AS rev
            """,
            {
                "repo_id":         repo_id,
                "commit_hash":     commit_hash,
                "tree_hash":       tree_hash,
                "parent_hashes":   parent_hashes,  # psycopg maps list -> text[]
                "author_name":     author_name,
                "author_email":    author_email,
                "authored_at":     authored_at,
                "committer_name":  committer_name,
                "committer_email": committer_email,
                "committed_at":    committed_at,
                "message":         message,
            },
        )
        rev = cur.fetchone()[0]

        # 3. Insert repo_objects rows so the blobs are tracked per-repo.
        #    ON CONFLICT DO NOTHING because the same blob can appear in
        #    many commits (and possibly many repos that share a store).
        if path_to_blob:
            cur.executemany(
                """
                INSERT INTO repo_objects
                    (object_hash, repo_id, byte_offset, size_bytes, obj_type)
                VALUES
                    (%s, %s, 0, 0, 'blob')
                ON CONFLICT (object_hash) DO NOTHING
                """,
                [(h, repo_id) for h in set(path_to_blob.values())],
            )

        # 4. Insert changesets. For imports we record every file as 'add'
        #    at its commit; real add/modify/delete diffs would require
        #    comparing trees between parent and this commit. That's a
        #    future optimization — the data we've stored is sufficient
        #    to compute real diffs on demand later.
        if path_to_blob:
            cur.executemany(
                """
                INSERT INTO repo_changesets
                    (commit_hash, path, change_type,
                     blob_before, blob_after,
                     lines_added, lines_removed)
                VALUES (%s, %s, 'add', NULL, %s, 0, 0)
                ON CONFLICT (commit_hash, path) DO NOTHING
                """,
                [(commit_hash, path, blob_hash)
                 for path, blob_hash in path_to_blob.items()],
            )

    return {
        "commit_hash":  commit_hash,
        "rev":          rev,
        "files_written": files_written,
    }