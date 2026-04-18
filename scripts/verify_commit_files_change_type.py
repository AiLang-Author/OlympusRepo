"""
verify_commit_files_change_type.py — regression test for the commit_files
change_type bug fixed in olympuscodereview.md blocker #3.

Bug: commit_files() mutated `tree` with new files BEFORE computing
change_type. The expression
    change_type = "modify" if filepath in (tree.keys() - {f for f, _ in files}) else "add"
always evaluated to "add" because every filepath-being-committed was
subtracted from tree.keys() before membership check.

This script:
  1. Creates a throwaway repo + user
  2. Commits foo.txt = "A"                  (expect change_type='add')
  3. Commits foo.txt = "B"                  (expect change_type='modify')
  4. Queries repo_changesets for the second commit
  5. Prints PASS if change_type is "modify", FAIL if "add"
  6. Cleans up (deletes repo, user)

Usage:
    python scripts/verify_commit_files_change_type.py

Requires: database reachable via env vars (same config olympusrepo uses),
a tmp objects dir, and olympusrepo importable.
"""
import os
import sys
import tempfile
import uuid

# Locate the package so this runs from anywhere inside the repo
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from olympusrepo.core import db, repo


def main() -> int:
    conn = db.connect()
    # Unique suffix so parallel/repeat runs don't collide on repo/user name
    suffix = uuid.uuid4().hex[:8]
    repo_name = f"verify_{suffix}"
    user_name = f"verifyuser_{suffix}"

    # Create a temp objects dir scoped to this run
    tmp_objects = tempfile.mkdtemp(prefix="verify_objects_")

    created_repo_id = None
    created_user_id = None

    try:
        # 1. Create user
        user = db.get_user_by_name(conn, user_name)
        if not user:
            # Minimal user record; bypassing any password workflow is fine —
            # we only need it to own the repo for this test.
            db.execute(conn, """
                INSERT INTO repo_users (username, password_hash, role)
                VALUES (%s, %s, 'mortal')
            """, (user_name, "verify_not_a_real_hash"))
            conn.commit()
            user = db.get_user_by_name(conn, user_name)
        created_user_id = user["user_id"]

        # 2. Create repo
        result = repo.create_repo(conn, repo_name, created_user_id,
                                  visibility="private")
        created_repo_id = result["repo_id"]

        # 3. First commit: foo.txt = "A"  (expected change_type='add')
        first = repo.commit_files(
            conn, created_repo_id, created_user_id,
            message="initial",
            files=[("foo.txt", b"A")],
            objects_dir=tmp_objects,
        )
        if not first:
            print("FAIL: first commit_files returned None")
            return 2
        conn.commit()

        # 4. Second commit: foo.txt = "B"  (expected change_type='modify')
        second = repo.commit_files(
            conn, created_repo_id, created_user_id,
            message="overwrite",
            files=[("foo.txt", b"B")],
            objects_dir=tmp_objects,
        )
        if not second:
            print("FAIL: second commit_files returned None")
            return 2
        conn.commit()

        # 5. Query change_type from repo_changesets
        row = db.query_one(conn, """
            SELECT change_type
              FROM repo_changesets
             WHERE commit_hash = %s AND path = %s
        """, (second["commit_hash"], "foo.txt"))

        if not row:
            print("FAIL: no changeset row found for the second commit")
            return 2

        actual = row["change_type"]
        print(f"Second commit change_type = {actual!r}")

        if actual == "modify":
            print("PASS: overwrite correctly recorded as 'modify'")
            return 0
        else:
            print(f"FAIL: expected 'modify', got {actual!r}")
            print("(this is the exact bug from olympuscodereview.md #3)")
            return 1

    finally:
        # Cleanup so verify can be re-run cleanly. Delete in FK order.
        if created_repo_id is not None:
            try:
                # Rely on cascade or be explicit — be explicit to avoid
                # blocker #5 ambiguity.
                db.execute(conn,
                    "DELETE FROM repo_changesets WHERE commit_hash IN "
                    "(SELECT commit_hash FROM repo_commits WHERE repo_id = %s)",
                    (created_repo_id,))
                db.execute(conn,
                    "DELETE FROM repo_refs WHERE repo_id = %s",
                    (created_repo_id,))
                db.execute(conn,
                    "DELETE FROM repo_commits WHERE repo_id = %s",
                    (created_repo_id,))
                db.execute(conn,
                    "DELETE FROM repo_objects WHERE repo_id = %s",
                    (created_repo_id,))
                db.execute(conn,
                    "DELETE FROM repo_repositories WHERE repo_id = %s",
                    (created_repo_id,))
                conn.commit()
            except Exception as e:
                print(f"cleanup: repo delete failed: {e}")
                conn.rollback()

        if created_user_id is not None:
            try:
                # Drop audit log entries first — any commit activity during
                # the run writes here, and repo_audit_log.user_id references
                # repo_users with no cascade.
                db.execute(conn,
                    "DELETE FROM repo_audit_log WHERE user_id = %s",
                    (created_user_id,))
                db.execute(conn,
                    "DELETE FROM repo_users WHERE user_id = %s",
                    (created_user_id,))
                conn.commit()
            except Exception as e:
                print(f"cleanup: user delete failed: {e}")
                conn.rollback()

        conn.close()

        # Remove temp objects dir
        import shutil
        shutil.rmtree(tmp_objects, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
