
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License
#
# Usage: python -m olympusrepo <command> [options]
#   or:  olympusrepo <command> [options]  (if installed as entry point)

import argparse
import os
import sys

from .core import db, objects, worktree, repo, diff


def cmd_init(args):
    """Create a new repository."""
    name = args.name
    conn = db.connect()
    try:
        # Get or create user (for local-only use, we need a user context)
        requested_user = args.user or os.getenv("USER", "zeus")
        user = db.get_user_by_name(conn, requested_user)
        if not user:
            print(f"ERROR: User '{requested_user}' not found.")
            print("  Create one first: olympusrepo user-create <username> <password>")
            return 1

        result = repo.create_repo(conn, name, user["user_id"],
                                  visibility=args.visibility or "public")
        print(f"Repository created: {result['name']}")
        print(f"  Visibility: {result['visibility']}")
        print(f"  Default branch: {result['default_branch']}")

        # Init local working directory
        repo_root = os.path.abspath(args.path or name)
        os.makedirs(repo_root, exist_ok=True)

        worktree.init_local(repo_root, {
            "repo_name": name,
            "repo_id": result["repo_id"],
            "default_branch": result["default_branch"],
            "user": user["username"],
            "user_id": user["user_id"],
        })

        # Create local objects directory
        objects_dir = os.path.join(repo_root, ".olympusrepo", "objects")
        os.makedirs(objects_dir, exist_ok=True)

        print(f"  Local directory: {repo_root}")
        print("Ready for commits.")
        return 0
    finally:
        conn.close()


def cmd_add(args):
    """Track files for commit."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        print("  Run 'olympusrepo init <name>' first.")
        return 1

    objects_dir = os.path.join(repo_root, ".olympusrepo", "objects")

    targets = args.files or ["."]

    added_count = 0
    for target in targets:
        if target == ".":
            files = worktree.scan_working_tree(repo_root)
        elif os.path.isdir(os.path.join(repo_root, target)):
            files = [f for f in worktree.scan_working_tree(repo_root)
                     if f.startswith(target)]
        else:
            files = [target]

        for filepath in files:
            full_path = os.path.join(repo_root, filepath)
            if not os.path.exists(full_path):
                print(f"  WARNING: {filepath} does not exist, skipping")
                continue

            obj_hash = objects.store_file(full_path, objects_dir)
            worktree.update_index_entry(repo_root, filepath, obj_hash)
            added_count += 1

    print(f"Added {added_count} file(s) to index.")
    return 0


def cmd_commit(args):
    """Save changes to the repository."""
    if not args.message:
        print("ERROR: Commit message required.")
        print('  Usage: olympusrepo commit -m "your message"')
        return 1

    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    config = worktree.load_config(repo_root)
    objects_dir = os.path.join(repo_root, ".olympusrepo", "objects")

    conn = db.connect()
    try:
        result = repo.commit(
            conn,
            repo_id=config["repo_id"],
            user_id=config["user_id"],
            message=args.message,
            repo_root=repo_root,
            objects_dir=objects_dir,
        )

        if result:
            print(f"Committed rev {result['rev']}  {result['commit_hash'][:12]}")
            print(f"  {result['files_changed']} file(s) changed")
            print(f"  {result['message']}")
        return 0
    except Exception as e:
        print(f"ERROR: Commit failed: {e}")
        return 1
    finally:
        conn.close()


def cmd_status(args):
    """Show what's changed in the working tree."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    objects_dir = os.path.join(repo_root, ".olympusrepo", "objects")
    changes = worktree.detect_changes(repo_root, objects_dir)

    branch = worktree.get_current_branch(repo_root)
    print(f"On branch: {branch}\n")

    if not any(changes.values()):
        print("Working tree clean — nothing to commit.")
        return 0

    if changes["modified"]:
        print("Modified:")
        for path, old_h, new_h in changes["modified"]:
            print(f"  {path}")

    if changes["added"]:
        print("New files:")
        for path, h in changes["added"]:
            print(f"  {path}")

    if changes["deleted"]:
        print("Deleted:")
        for path, h in changes["deleted"]:
            print(f"  {path}")

    total = len(changes["modified"]) + len(changes["added"]) + len(changes["deleted"])
    print(f"\n{total} change(s) total.")
    return 0


def cmd_log(args):
    """Show commit history."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    config = worktree.load_config(repo_root)
    conn = db.connect()
    try:
        commits = repo.get_log(conn, config["repo_id"],
                               limit=args.limit, path=args.path)

        if not commits:
            print("No commits yet.")
            return 0

        for c in commits:
            print(f"  rev {c['rev']}  {c['commit_hash'][:12]}")
            print(f"  Author: {c['author_name']}")
            print(f"  Date:   {c['committed_at']}")
            print(f"  {c['message']}")
            print()

        return 0
    finally:
        conn.close()


def cmd_diff(args):
    """Show differences in working tree."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    objects_dir = os.path.join(repo_root, ".olympusrepo", "objects")
    changes = worktree.detect_changes(repo_root, objects_dir)

    if not changes["modified"]:
        print("No modifications to show.")
        return 0

    # If a specific file was requested, filter to that
    target = args.file
    modified = changes["modified"]
    if target:
        modified = [m for m in modified if m[0] == target]
        if not modified:
            print(f"No modifications in: {target}")
            return 0

    for filepath, old_hash, new_hash in modified:
        old_content = objects.retrieve_blob(old_hash, objects_dir)
        if old_content is None:
            print(f"--- {filepath}: cannot retrieve old version")
            continue

        new_path = os.path.join(repo_root, filepath)
        with open(new_path, "r", errors="replace") as f:
            new_content = f.read()

        diff_text, added, removed = diff.diff_content(
            old_content.decode("utf-8", errors="replace"),
            new_content, filepath, filepath
        )

        if diff_text:
            print(diff_text)
            print(f"  +{added} -{removed}")
            print()

    return 0


def cmd_branch(args):
    """Create or list branches."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    config = worktree.load_config(repo_root)
    conn = db.connect()
    try:
        if not args.name:
            branches = repo.get_branches(conn, config["repo_id"])
            current = worktree.get_current_branch(repo_root)
            for b in branches:
                ref = b["ref_name"].replace("refs/heads/", "")
                marker = " *" if ref == current else "  "
                print(f"{marker} {ref}")
            return 0

        current = worktree.get_current_branch(repo_root)
        try:
            result = repo.create_branch(conn, config["repo_id"],
                                        config["user_id"], args.name,
                                        from_branch=current)
            print(f"Branch created: {result['branch_name']}  (from {result['from']})")
            return 0
        except Exception as e:
            print(f"ERROR: Could not create branch: {e}")
            return 1
    finally:
        conn.close()


def cmd_switch(args):
    """Switch to a branch."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    config = worktree.load_config(repo_root)
    conn = db.connect()
    try:
        # Verify the branch exists
        ref = f"refs/heads/{args.branch}"
        row = db.query_one(conn,
            "SELECT 1 FROM repo_refs WHERE repo_id = %s AND ref_name = %s",
            (config["repo_id"], ref))
        if not row:
            print(f"ERROR: Branch '{args.branch}' does not exist.")
            print(f"  Create it first: olympusrepo branch {args.branch}")
            return 1
    finally:
        conn.close()

    worktree.set_current_branch(repo_root, args.branch)
    print(f"Switched to: {args.branch}")
    return 0


def cmd_resolve(args):
    """Mark a conflicted file as resolved."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    filepath = args.file
    full_path = os.path.join(repo_root, filepath)

    if not os.path.exists(full_path):
        print(f"ERROR: File not found: {filepath}")
        return 1

    with open(full_path, "r", errors="replace") as f:
        content = f.read()

    if diff.has_conflict_markers(content):
        count = diff.count_conflicts(content)
        print(f"WARNING: {filepath} still contains {count} conflict marker(s).")
        print("  Edit the file and remove all <<<<<<< / ======= / >>>>>>> markers first.")
        return 1

    print(f"Resolved: {filepath}")
    return 0


def cmd_user_create(args):
    """Create a new user (admin tool)."""
    conn = db.connect()
    try:
        try:
            user_id = db.create_user(conn, args.username, args.password,
                                     role=args.role, full_name=args.full_name)
            conn.commit()
        except ValueError as e:
            print(f"ERROR: {e}")
            return 1
        except Exception as e:
            conn.rollback()
            print(f"ERROR: Could not create user: {e}")
            return 1
        print(f"User created: {args.username} (role: {args.role}, id: {user_id})")
        return 0
    finally:
        conn.close()


# =========================================================================
# MAIN
# =========================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="olympusrepo",
        description="OlympusRepo — Sovereign Version Control",
        epilog="Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering"
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # init
    p = sub.add_parser("init", help="Create a new repository")
    p.add_argument("name", help="Repository name")
    p.add_argument("--path", help="Local directory (default: ./<name>)")
    p.add_argument("--visibility", choices=["public", "private", "internal"], default="public")
    p.add_argument("--user", help="Username (default: $USER)")

    # add
    p = sub.add_parser("add", help="Track files for commit")
    p.add_argument("files", nargs="*", help="Files or directories to add (default: .)")

    # commit
    p = sub.add_parser("commit", help="Save your changes")
    p.add_argument("-m", "--message", required=True, help="Commit message")

    # status
    sub.add_parser("status", help="What's changed?")

    # log
    p = sub.add_parser("log", help="Show commit history")
    p.add_argument("--limit", type=int, default=20, help="Max commits to show")
    p.add_argument("--path", help="Filter by file path")

    # diff
    p = sub.add_parser("diff", help="Show differences")
    p.add_argument("file", nargs="?", help="Specific file to diff")

    # branch
    p = sub.add_parser("branch", help="Create or list branches")
    p.add_argument("name", nargs="?", help="Branch name (omit to list)")

    # switch
    p = sub.add_parser("switch", help="Switch to a branch")
    p.add_argument("branch", help="Branch name")

    # resolve
    p = sub.add_parser("resolve", help="Mark a conflicted file as resolved")
    p.add_argument("file", help="File to mark resolved")

    # user-create (admin)
    p = sub.add_parser("user-create", help="Create a new user")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("--role", default="mortal",
                   choices=["zeus", "olympian", "titan", "mortal", "prometheus", "hermes"])
    p.add_argument("--full-name")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    commands = {
        "init": cmd_init,
        "add": cmd_add,
        "commit": cmd_commit,
        "status": cmd_status,
        "log": cmd_log,
        "diff": cmd_diff,
        "branch": cmd_branch,
        "switch": cmd_switch,
        "resolve": cmd_resolve,
        "user-create": cmd_user_create,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    else:
        print(f"Unknown command: {args.command}")
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
