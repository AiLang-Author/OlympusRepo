
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License

import json
import os
import time

from . import db, objects, worktree, diff


def create_repo(conn, name: str, owner_id: int, visibility: str = "public",
                description: str = None) -> dict:
    """Create a new repository in the database (single transaction)."""
    try:
        row = db.query_one(conn, """
            INSERT INTO repo_repositories (name, owner_id, visibility, description)
            VALUES (%s, %s, %s, %s)
            RETURNING repo_id, name, visibility, default_branch
        """, (name, owner_id, visibility, description))

        # Create default branch ref (no commit yet)
        db.execute(conn, """
            INSERT INTO repo_refs (repo_id, ref_name, updated_by)
            VALUES (%s, %s, %s)
        """, (row["repo_id"], f"refs/heads/{row['default_branch']}", owner_id),
            commit=False)

        db.audit_log(conn, "repo_create", user_id=owner_id,
                     repo_id=row["repo_id"], target_type="repo", target_id=name,
                     commit=False)

        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise


def get_repo(conn, name: str) -> dict | None:
    """Get repository by name."""
    return db.query_one(conn,
        "SELECT * FROM repo_repositories WHERE name = %s", (name,))


def list_repos(conn, user_id: int = None) -> list[dict]:
    """List repositories visible to a user (or public only if no user)."""
    if user_id:
        return db.query(conn, """
            SELECT r.* FROM repo_repositories r
            WHERE r.visibility = 'public'
               OR r.visibility = 'internal'
               OR r.owner_id = %s
               OR r.repo_id IN (SELECT repo_id FROM repo_access WHERE user_id = %s)
            ORDER BY r.updated_at DESC
        """, (user_id, user_id))
    else:
        return db.query(conn,
            "SELECT * FROM repo_repositories WHERE visibility = 'public' ORDER BY updated_at DESC")


# =========================================================================
# COMMIT
# =========================================================================

def _bump_file_revs(conn, repo_id: int, commit_hash: str,
                    global_rev: int, changes: dict,
                    author_name: str = None, message: str = None):
    """
    Record a file revision entry for every changed file.
    Uses committed_at timestamp as the version identifier.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    all_changes = (
        [(path, new_hash, 'add')
         for path, new_hash in changes.get('added', [])] +
        [(path, new_hash, 'modify')
         for path, old_hash, new_hash in changes.get('modified', [])] +
        [(path, None, 'delete')
         for path, old_hash in changes.get('deleted', [])]
    )

    for path, blob_hash, change_type in all_changes:
        db.execute(conn, """
            INSERT INTO repo_file_revisions
                (repo_id, path, blob_hash, commit_hash, global_rev,
                 change_type, committed_at, author_name, message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (repo_id, path, blob_hash or '',
              commit_hash, global_rev, change_type,
              now, author_name, message), commit=False)


def commit(conn, repo_id: int, user_id: int, message: str,
           repo_root: str, objects_dir: str) -> dict | None:
    """
    Create a commit from the current working tree changes.

    All DB writes happen in a single transaction. If anything fails,
    nothing is committed to the DB. Blob storage is idempotent so failed
    commits don't leave the object store in a bad state either.

    1. Detect changes
    2. Store blobs in object store (idempotent, safe on retry)
    3. Build tree hash
    4. Begin transaction
    5. Create commit record
    6. Create changeset records
    7. Update branch ref
    8. Audit log
    9. Commit transaction
    10. Update local index (only after DB commit succeeds)
    """
    user = db.get_user(conn, user_id)
    if not user:
        print("ERROR: User not found")
        return None

    index = worktree.load_index(repo_root)
    branch = worktree.get_current_branch(repo_root)
    ref_name = f"refs/heads/{branch}"

    # Replay all changesets up to HEAD to build prev_tree
    existing = db.query(conn, """
        SELECT cs.path, cs.blob_after, cs.change_type
          FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
         WHERE c.repo_id = %s
         ORDER BY c.rev ASC, cs.path ASC
    """, (repo_id,))

    prev_tree = {}
    for row in existing:
        if row["change_type"] in ("add", "modify"):
            prev_tree[row["path"]] = row["blob_after"]
        elif row["change_type"] == "delete":
            prev_tree.pop(row["path"], None)

    print(f"DEBUG commit: index={len(index)} files, prev_tree={len(prev_tree)} files")

    # Diff index against prev_tree
    changes = {"added": [], "modified": [], "deleted": []}
    for filepath, entry in index.items():
        new_hash = entry["hash"]
        if filepath not in prev_tree:
            changes["added"].append((filepath, new_hash))
        elif prev_tree[filepath] != new_hash:
            changes["modified"].append((filepath, prev_tree[filepath], new_hash))
            
    for filepath, old_hash in prev_tree.items():
        if filepath not in index:
            changes["deleted"].append((filepath, old_hash))

    total_changes = len(changes["modified"]) + len(changes["added"]) + len(changes["deleted"])
    if total_changes == 0:
        print("Nothing to commit.")
        return None

    # Tree hash = hash of sorted entries
    tree_entries = {k: v["hash"] for k, v in index.items()}
    tree_content = json.dumps(
        {k: v for k, v in sorted(tree_entries.items())},
        sort_keys=True
    ).encode()
    tree_hash = objects.hash_content(tree_content)
    objects.store_blob(tree_content, objects_dir)

    # Commit hash = hash of tree + parent + message + timestamp
    parent_row = db.query_one(conn,
        "SELECT commit_hash FROM repo_refs WHERE repo_id = %s AND ref_name = %s",
        (repo_id, ref_name))
    parent_hash = parent_row["commit_hash"] if parent_row else None

    ts = str(time.time())
    commit_content = f"{tree_hash}\n{parent_hash or 'none'}\n{user['username']}\n{ts}\n{message}"
    commit_hash = objects.hash_content(commit_content.encode())

    # parent_hashes is a TEXT[] — pass a Python list, let psycopg2 adapt it
    parent_hashes = [parent_hash] if parent_hash else None

    # =========================================================
    # Begin transaction: all DB writes atomic as a single unit
    # =========================================================
    try:
        # Insert commit
        db.execute(conn, """
            INSERT INTO repo_commits
                (commit_hash, repo_id, tree_hash, author_id, author_name,
                 committer_id, committer_name, message, parent_hashes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (commit_hash, repo_id, tree_hash, user_id, user["username"],
              user_id, user["username"], message, parent_hashes),
            commit=False)

        # Insert changesets
        for filepath, new_hash in changes["added"]:
            db.execute(conn, """
                INSERT INTO repo_changesets (commit_hash, path, change_type, blob_after)
                VALUES (%s, %s, 'add', %s)
            """, (commit_hash, filepath, new_hash), commit=False)

        for filepath, old_hash, new_hash in changes["modified"]:
            # Get diff stats
            old_content = objects.retrieve_blob(old_hash, objects_dir)
            new_content = objects.retrieve_blob(new_hash, objects_dir)

            if old_content and new_content:
                _, added, removed = diff.diff_content(
                    old_content.decode("utf-8", errors="replace"),
                    new_content.decode("utf-8", errors="replace") if new_content else "",
                    filepath, filepath
                )
            else:
                added, removed = 0, 0

            db.execute(conn, """
                INSERT INTO repo_changesets
                    (commit_hash, path, change_type, blob_before, blob_after,
                     lines_added, lines_removed)
                VALUES (%s, %s, 'modify', %s, %s, %s, %s)
            """, (commit_hash, filepath, old_hash, new_hash, added, removed),
                commit=False)

        for filepath, old_hash in changes["deleted"]:
            db.execute(conn, """
                INSERT INTO repo_changesets (commit_hash, path, change_type, blob_before)
                VALUES (%s, %s, 'delete', %s)
            """, (commit_hash, filepath, old_hash), commit=False)

        # Update branch ref
        db.execute(conn, """
            UPDATE repo_refs
               SET commit_hash = %s, updated_at = NOW(), updated_by = %s
             WHERE repo_id = %s AND ref_name = %s
        """, (commit_hash, user_id, repo_id, ref_name), commit=False)

        # Audit log
        db.audit_log(conn, "commit_staging", user_id=user_id, repo_id=repo_id,
                     target_type="commit", target_id=commit_hash,
                     details={"message": message, "files_changed": total_changes},
                     commit=False)

        # Bump repository updated_at
        db.execute(conn,
            "UPDATE repo_repositories SET updated_at = NOW() WHERE repo_id = %s",
            (repo_id,), commit=False)

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    # Auto-link issue references from commit message
    try:
        from .app import _parse_issue_refs  # avoid circular import
    except ImportError:
        pass

    # Inline the parser to avoid circular import
    import re
    issue_patterns = [
        (r'(?:fixes|fix|closes|close|resolves|resolve)\s+#(\d+)', 'fixed'),
        (r'(?:introduces|introduced)\s+#(\d+)', 'introduced'),
        (r'(?:relates|related|see)\s+#(\d+)', 'related'),
        (r'#(\d+)', 'mentioned'),
    ]
    seen_issues = set()
    for pattern, link_type in issue_patterns:
        for match in re.finditer(pattern, message, re.IGNORECASE):
            num = int(match.group(1))
            if num in seen_issues:
                continue
            seen_issues.add(num)
            issue_row = db.query_one(conn,
                "SELECT issue_id FROM repo_issues WHERE repo_id = %s AND number = %s",
                (repo_id, num))
            if issue_row:
                try:
                    db.execute(conn, """
                        INSERT INTO repo_issue_commits
                            (issue_id, commit_hash, link_type)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (issue_row["issue_id"], commit_hash, link_type))
                    if link_type == 'fixed':
                        db.execute(conn, """
                            UPDATE repo_issues
                               SET status = 'resolved', closed_at = NOW()
                             WHERE issue_id = %s
                        """, (issue_row["issue_id"],))
                except Exception:
                    pass

    rev = db.query_scalar(conn,
        "SELECT rev FROM repo_commits WHERE commit_hash = %s", (commit_hash,))

    # Bump file-level revision counters
    try:
        _bump_file_revs(conn, repo_id, commit_hash, rev, changes, user["username"], message)
        conn.commit()
    except Exception as e:
        print(f"WARNING: _bump_file_revs failed: {e}")
        conn.rollback()

    return {
        "commit_hash": commit_hash,
        "rev": rev,
        "tree_hash": tree_hash,
        "files_changed": total_changes,
        "message": message,
    }


def commit_files(conn, repo_id: int, user_id: int, message: str,
                 files: list[tuple[str, bytes]], objects_dir: str) -> dict | None:
    """
    Create a commit from raw file bytes (used by web upload).
    files is a list of (filepath, content_bytes) tuples.
    """
    user = db.get_user(conn, user_id)
    if not user:
        return None
    if not files:
        return None

    # Store blobs
    for filepath, content in files:
        objects.store_blob(content, objects_dir)

    # Get current tree from latest commit on default branch
    branch = db.query_one(conn,
        "SELECT default_branch FROM repo_repositories WHERE repo_id = %s",
        (repo_id,))
    default_branch = branch["default_branch"] if branch else "main"
    ref_name = f"refs/heads/{default_branch}"

    # Replay existing tree
    existing = db.query(conn, """
        SELECT cs.path, cs.blob_after, cs.change_type
          FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
         WHERE c.repo_id = %s
         ORDER BY c.rev ASC, cs.path ASC
    """, (repo_id,))

    tree = {}
    for row in existing:
        if row["change_type"] in ("add", "modify"):
            tree[row["path"]] = row["blob_after"]
        elif row["change_type"] == "delete":
            tree.pop(row["path"], None)

    # Apply new files
    new_hashes = {}
    for filepath, content in files:
        h = objects.hash_content(content)
        tree[filepath] = h
        new_hashes[filepath] = h

    # Build tree hash
    tree_content = json.dumps(
        {k: v for k, v in sorted(tree.items())},
        sort_keys=True
    ).encode()
    tree_hash = objects.hash_content(tree_content)
    objects.store_blob(tree_content, objects_dir)

    # Get parent commit
    parent_row = db.query_one(conn,
        "SELECT commit_hash FROM repo_refs WHERE repo_id = %s AND ref_name = %s",
        (repo_id, ref_name))
    parent_hash = parent_row["commit_hash"] if parent_row else None
    parent_hashes = [parent_hash] if parent_hash else None

    # Build commit hash
    ts = str(time.time())
    commit_content = f"{tree_hash}\n{parent_hash or 'none'}\n{user['username']}\n{ts}\n{message}"
    commit_hash = objects.hash_content(commit_content.encode())

    try:
        db.execute(conn, """
            INSERT INTO repo_commits
                (commit_hash, repo_id, tree_hash, author_id, author_name,
                 committer_id, committer_name, message, parent_hashes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (commit_hash, repo_id, tree_hash, user_id, user["username"],
              user_id, user["username"], message, parent_hashes), commit=False)

        for filepath, content in files:
            h = new_hashes[filepath]
            change_type = "modify" if filepath in (tree.keys() - {f for f, _ in files}) else "add"
            db.execute(conn, """
                INSERT INTO repo_changesets
                    (commit_hash, path, change_type, blob_after)
                VALUES (%s, %s, %s, %s)
            """, (commit_hash, filepath, change_type, h), commit=False)

            # Record in repo_objects
            try:
                db.execute(conn, """
                    INSERT INTO repo_objects
                        (object_hash, repo_id, byte_offset, size_bytes, obj_type)
                    VALUES (%s, %s, 0, %s, 'blob')
                    ON CONFLICT (object_hash) DO NOTHING
                """, (h, repo_id, len(content)), commit=False)
            except Exception:
                pass

        # Update ref
        db.execute(conn, """
            UPDATE repo_refs
               SET commit_hash = %s, updated_at = NOW(), updated_by = %s
             WHERE repo_id = %s AND ref_name = %s
        """, (commit_hash, user_id, repo_id, ref_name), commit=False)

        db.audit_log(conn, "commit_upload", user_id=user_id, repo_id=repo_id,
                     target_type="commit", target_id=commit_hash,
                     details={"message": message, "files": len(files)},
                     commit=False)

        db.execute(conn,
            "UPDATE repo_repositories SET updated_at = NOW() WHERE repo_id = %s",
            (repo_id,), commit=False)

        conn.commit()

        # Snapshot committed state for diff/status
        # Save full index format (with mtime/size) for fast-path diff detection
        worktree.save_committed_index(repo_root, worktree.load_index(repo_root))

    except Exception:
        conn.rollback()
        raise

    # Auto-link issue references from commit message
    try:
        from .app import _parse_issue_refs  # avoid circular import
    except ImportError:
        pass

    # Inline the parser to avoid circular import
    import re
    issue_patterns = [
        (r'(?:fixes|fix|closes|close|resolves|resolve)\s+#(\d+)', 'fixed'),
        (r'(?:introduces|introduced)\s+#(\d+)', 'introduced'),
        (r'(?:relates|related|see)\s+#(\d+)', 'related'),
        (r'#(\d+)', 'mentioned'),
    ]
    seen_issues = set()
    for pattern, link_type in issue_patterns:
        for match in re.finditer(pattern, message, re.IGNORECASE):
            num = int(match.group(1))
            if num in seen_issues:
                continue
            seen_issues.add(num)
            issue_row = db.query_one(conn,
                "SELECT issue_id FROM repo_issues WHERE repo_id = %s AND number = %s",
                (repo_id, num))
            if issue_row:
                try:
                    db.execute(conn, """
                        INSERT INTO repo_issue_commits
                            (issue_id, commit_hash, link_type)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (issue_row["issue_id"], commit_hash, link_type))
                    if link_type == 'fixed':
                        db.execute(conn, """
                            UPDATE repo_issues
                               SET status = 'resolved', closed_at = NOW()
                             WHERE issue_id = %s
                        """, (issue_row["issue_id"],))
                except Exception:
                    pass

    rev = db.query_scalar(conn,
        "SELECT rev FROM repo_commits WHERE commit_hash = %s", (commit_hash,))

    # Build changes dict format for _bump_file_revs
    file_changes = {
        'added': [(path, h) for path, h in new_hashes.items()],
        'modified': [],
        'deleted': []
    }
    try:
        _bump_file_revs(conn, repo_id, commit_hash, rev, file_changes, user["username"], message)
        conn.commit()
    except Exception as e:
        print(f"WARNING: _bump_file_revs failed: {e}")
        conn.rollback()

    return {
        "commit_hash": commit_hash,
        "rev": rev,
        "files_uploaded": len(files),
        "message": message,
    }


def get_log(conn, repo_id: int, limit: int = 20, path: str = None) -> list[dict]:
    """Get commit history."""
    if path:
        return db.query(conn, """
            SELECT c.rev, c.commit_hash, c.author_name, c.message, c.committed_at,
                   cs.change_type, cs.lines_added, cs.lines_removed
              FROM repo_commits c
              JOIN repo_changesets cs ON cs.commit_hash = c.commit_hash
             WHERE c.repo_id = %s AND cs.path = %s
             ORDER BY c.rev DESC LIMIT %s
        """, (repo_id, path, limit))
    else:
        return db.query(conn, """
            SELECT rev, commit_hash, author_name, message, committed_at
              FROM repo_commits WHERE repo_id = %s
             ORDER BY rev DESC LIMIT %s
        """, (repo_id, limit))


def get_branches(conn, repo_id: int) -> list[dict]:
    """List branches for a repo."""
    return db.query(conn, """
        SELECT ref_name, commit_hash, updated_at
          FROM repo_refs WHERE repo_id = %s
         ORDER BY ref_name
    """, (repo_id,))


def create_branch(conn, repo_id: int, user_id: int, branch_name: str,
                  from_branch: str = None) -> dict:
    """
    Create a new branch pointing at from_branch's current commit (or at
    the repo's default branch if from_branch is None).

    Single transaction: creates the ref, writes audit log.
    Raises on failure.
    """
    if from_branch is None:
        repo_row = db.query_one(conn,
            "SELECT default_branch FROM repo_repositories WHERE repo_id = %s",
            (repo_id,))
        from_branch = repo_row["default_branch"] if repo_row else "main"

    from_ref = f"refs/heads/{from_branch}"
    new_ref = f"refs/heads/{branch_name}"

    head = db.query_one(conn,
        "SELECT commit_hash FROM repo_refs WHERE repo_id = %s AND ref_name = %s",
        (repo_id, from_ref))
    commit_hash = head["commit_hash"] if head else None

    try:
        db.execute(conn, """
            INSERT INTO repo_refs (repo_id, ref_name, commit_hash, updated_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (repo_id, ref_name)
            DO UPDATE SET commit_hash = EXCLUDED.commit_hash,
                          updated_at = NOW(),
                          updated_by = EXCLUDED.updated_by
        """, (repo_id, new_ref, commit_hash, user_id), commit=False)

        db.audit_log(conn, "branch_create", user_id=user_id, repo_id=repo_id,
                     target_type="ref", target_id=branch_name,
                     details={"from": from_branch, "commit_hash": commit_hash},
                     commit=False)

        conn.commit()

        return {
            "ref_name": new_ref,
            "branch_name": branch_name,
            "commit_hash": commit_hash,
            "from": from_branch,
        }
    except Exception:
        conn.rollback()
        raise


# =========================================================================
# PERMISSIONS
# =========================================================================

def check_permission(conn, user_id: int, repo_id: int, action: str,
                     scope: str = None) -> bool:
    """Check if user has permission to perform action on scope."""
    user = db.get_user(conn, user_id)
    if not user:
        return False

    repo_row = db.query_one(conn,
        "SELECT * FROM repo_repositories WHERE repo_id = %s", (repo_id,))
    if not repo_row:
        return False

    # Zeus (repo owner) or global zeus role always passes
    if repo_row["owner_id"] == user_id or user["role"] == "zeus":
        return True

    # Check direct user permission
    row = db.query_one(conn, """
        SELECT 1 FROM repo_permissions
         WHERE repo_id = %s AND user_id = %s AND action = %s
           AND (scope = '*' OR scope = %s)
    """, (repo_id, user_id, action, scope or "*"))
    if row:
        return True

    # Check role-level permission
    row = db.query_one(conn, """
        SELECT 1 FROM repo_permissions
         WHERE repo_id = %s AND role = %s AND action = %s
           AND (scope = '*' OR scope = %s)
    """, (repo_id, user["role"], action, scope or "*"))
    if row:
        return True

    return False


def check_visibility(conn, repo_id: int, user_id: int = None) -> bool:
    """Check if user can read this repo."""
    repo_row = db.query_one(conn,
        "SELECT * FROM repo_repositories WHERE repo_id = %s", (repo_id,))
    if not repo_row:
        return False

    if repo_row["visibility"] == "public":
        return True

    if user_id is None:
        return False

    if repo_row["visibility"] == "internal":
        return True

    if repo_row["owner_id"] == user_id:
        return True

    row = db.query_one(conn,
        "SELECT 1 FROM repo_access WHERE repo_id = %s AND user_id = %s",
        (repo_id, user_id))
    return row is not None
