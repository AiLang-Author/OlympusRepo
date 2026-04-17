
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License
#
# Usage: python -m olympusrepo <command> [options]
#   or:  olympusrepo <command> [options]  (if installed as entry point)

import argparse
import os
import sys
import urllib.request
import urllib.error
import base64
import json as json_mod

from .core import db, objects, worktree, repo, diff, repo_setup


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
        
        repo_setup.init_instance_user(conn)

        # Create local objects directory
        objects_dir = os.environ.get(
            "OLYMPUSREPO_OBJECTS_DIR",
            os.path.join(repo_root, ".olympusrepo", "objects")
        )
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

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(repo_root, ".olympusrepo", "objects")
    )

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
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(repo_root, ".olympusrepo", "objects")
    )

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
            print(f"Committed  {result['commit_hash'][:12]}")
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

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(repo_root, ".olympusrepo", "objects")
    )
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

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(repo_root, ".olympusrepo", "objects")
    )
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


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        return json_mod.loads(response.read().decode("utf-8"))


def _fetch_blob(url: str) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        return response.read()


def _post_json(url: str, payload: dict) -> dict:
    data = json_mod.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json_mod.loads(response.read().decode("utf-8"))


def _blob_exists_local(repo_root: str, obj_hash: str) -> bool:
    if not obj_hash:
        return True
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(repo_root, ".olympusrepo", "objects")
    )
    return objects.exists(obj_hash, objects_dir)


def cmd_clone(args):
    """Clone a repository from a remote OlympusRepo instance."""
    url = args.url.rstrip("/")
    
    # Parse: http://host:port/repo/name  OR  http://host:port
    # with separate --name arg
    if "/repo/" in url:
        base_url  = url[:url.index("/repo/")]
        repo_name = url.split("/repo/")[-1].split("/")[0]
    else:
        print("ERROR: URL must include /repo/<name> (e.g. http://host:port/repo/name)")
        return 1

    dest = args.dest or repo_name
    if os.path.exists(dest):
        print(f"ERROR: '{dest}' already exists.")
        return 1

    print(f"Cloning {repo_name} from {base_url}...")

    # Get repo info from canonical
    try:
        info = _fetch_json(f"{base_url}/api/sync/{repo_name}/info")
    except Exception as e:
        print(f"ERROR: Cannot reach {base_url}: {e}")
        return 1

    # Create local directory and init
    os.makedirs(dest, exist_ok=True)
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(dest, ".olympusrepo", "objects")
    )

    worktree.init_local(dest, {
        "repo_name":      info["repo_name"],
        "repo_id":        info["repo_id"],
        "default_branch": info["default_branch"],
        "server_url":     base_url,
        "user":           args.user or os.getenv("USER", ""),
        "user_id":        None,
    })
    os.makedirs(objects_dir, exist_ok=True)

    conn = db.connect()
    try:
        # Create repo record in local DB
        db.execute(conn, """
            INSERT INTO repo_repositories
                (repo_id, name, visibility, owner_id,
                 default_branch, description)
            VALUES (%s, %s, %s, 1, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                repo_id = EXCLUDED.repo_id,
                visibility = EXCLUDED.visibility,
                default_branch = EXCLUDED.default_branch
        """, (info["repo_id"], info["repo_name"],
              info["visibility"],
              info["default_branch"],
              f"Clone of {base_url}/repo/{repo_name}"))

        db.execute(conn, """
            INSERT INTO repo_refs (repo_id, ref_name, updated_by)
            VALUES (%s, %s, 1)
            ON CONFLICT DO NOTHING
        """, (info["repo_id"],
              f"refs/heads/{info['default_branch']}"))

        # Add origin remote
        db.execute(conn, """
            INSERT INTO repo_remotes (name, url, role)
            VALUES ('origin', %s, 'canonical')
            ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
        """, (base_url,))

        conn.commit()

        # Now pull all commits using existing pull logic
        # Temporarily patch config
        config = worktree.load_config(dest)

        commits = _fetch_json(
            f"{base_url}/api/sync/{repo_name}/commits?since_rev=0")

        from .core import objects as obj_store
        blobs_fetched   = 0
        commits_applied = 0

        print(f"  Fetching {len(commits)} commit(s)...")

        for c in commits:
            for cs in c.get("changesets", []):
                # Only fetch blob_after — blob_before is historical, not needed for clone
                bh = cs.get("blob_after")
                if bh and not _blob_exists_local(dest, bh):
                    content = _fetch_blob(
                        f"{base_url}/api/sync/{repo_name}/blob/{bh}")
                    if content is not None:
                        obj_store.store_blob(content, objects_dir)
                        blobs_fetched += 1

            try:
                db.execute(conn, """
                    INSERT INTO repo_commits
                        (commit_hash, repo_id, tree_hash,
                         author_name, committer_name, message,
                         committed_at, rev, parent_hashes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (commit_hash) DO NOTHING
                """, (c["commit_hash"], info["repo_id"],
                      c["tree_hash"], c["author_name"],
                      c["committer_name"], c["message"],
                      c["committed_at"], c["rev"],
                      c.get("parent_hashes")))

                for cs in c.get("changesets", []):
                    db.execute(conn, """
                        INSERT INTO repo_changesets
                            (commit_hash, path, change_type,
                             blob_before, blob_after,
                             lines_added, lines_removed)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """, (c["commit_hash"], cs["path"],
                          cs["change_type"],
                          cs.get("blob_before"),
                          cs.get("blob_after"),
                          cs.get("lines_added", 0),
                          cs.get("lines_removed", 0)))
                commits_applied += 1
            except Exception as e:
                conn.rollback()
                print(f"  WARNING: {e}")
                continue

        # Update ref to latest commit
        if commits:
            latest = commits[-1]
            db.execute(conn, """
                UPDATE repo_refs
                   SET commit_hash = %s, updated_at = NOW()
                 WHERE repo_id = %s AND ref_name = %s
            """, (latest["commit_hash"], info["repo_id"],
                  f"refs/heads/{info['default_branch']}"))

        conn.commit()

        # Build local index from latest file tree
        # Replay changesets to get current files
        tree = {}
        for c in commits:
            for cs in c.get("changesets", []):
                if cs["change_type"] in ("add", "modify"):
                    tree[cs["path"]] = cs.get("blob_after")
                elif cs["change_type"] == "delete":
                    tree.pop(cs["path"], None)

        # Update index
        index = {}
        for path, blob_hash in tree.items():
            if blob_hash:
                index[path] = {
                    "hash":  blob_hash,
                    "mtime": 0,
                    "size":  0,
                }
        worktree.save_index(dest, index)
        # Write files to working tree
        for fpath, entry in index.items():
            h = entry["hash"] if isinstance(entry, dict) else entry
            if not h:
                continue
            full_path = os.path.join(dest, fpath)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            obj_store.retrieve_to_file(h, full_path, objects_dir)
        # Committed index = same as staged after fresh clone
        worktree.save_committed_index(dest, index)

        print(f"  {commits_applied} commit(s), {blobs_fetched} blob(s)")
        print(f"  Cloned into ./{dest}/")
        repo_setup.post_clone_setup(dest, base_url, conn, username=args.user)
        return 0

    except Exception as e:
        print(f"ERROR: Clone failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        conn.close()


def cmd_remote(args):
    """Manage remote instances."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    config_path = os.path.join(repo_root, ".olympusrepo", "config.json")
    config = worktree.load_config(repo_root)
    remotes = config.setdefault("remotes", {})

    if args.remote_action == "add":
        if not args.remote_name or not args.url:
            print("ERROR: 'add' requires remote_name and url")
            return 1
        remotes[args.remote_name] = {"url": args.url, "role": args.role}
        with open(config_path, "w") as f:
            json_mod.dump(config, f, indent=2)
        print(f"Added remote '{args.remote_name}' ({args.url})")
        
    elif args.remote_action == "remove":
        if args.remote_name in remotes:
            del remotes[args.remote_name]
            with open(config_path, "w") as f:
                json_mod.dump(config, f, indent=2)
            print(f"Removed remote '{args.remote_name}'")
        else:
            print(f"ERROR: Remote '{args.remote_name}' not found.")
            return 1

    elif args.remote_action == "list":
        for name, rdata in remotes.items():
            print(f"{name}\t{rdata['url']} ({rdata['role']})")
            
    return 0


def cmd_pull(args):
    """Pull commits from canonical."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    config = worktree.load_config(repo_root)
    remotes = config.get("remotes", {})
    if args.remote not in remotes:
        print(f"ERROR: Remote '{args.remote}' not found.")
        return 1

    remote_url = remotes[args.remote]["url"].rstrip("/")
    repo_name = config.get("repo_name")
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(repo_root, ".olympusrepo", "objects")
    )

    conn = db.connect()
    try:
        # Use last_synced_rev from config — never derived from NULL-rev local commits
        local_rev = config.get("last_synced_rev", 0) or 0

        print(f"Fetching from {remote_url}...")
        info = _fetch_json(f"{remote_url}/api/sync/{repo_name}/info")
        
        # Auto-create repo record in slave DB if missing
        local_repo = db.query_one(conn,
            "SELECT repo_id FROM repo_repositories WHERE name = %s",
            (repo_name,))
        if not local_repo:
            db.execute(conn, """
                INSERT INTO repo_repositories
                    (repo_id, name, visibility, owner_id,
                     default_branch, description)
                VALUES (%s, %s, %s, 1, %s, %s)
                ON CONFLICT (repo_id) DO NOTHING
            """, (info["repo_id"], info["repo_name"],
                  info["visibility"],
                  info["default_branch"],
                  f"Mirror of {info['repo_name']}"))
            db.execute(conn, """
                INSERT INTO repo_refs (repo_id, ref_name, updated_by)
                VALUES (%s, %s, 1)
                ON CONFLICT DO NOTHING
            """, (info["repo_id"],
                  f"refs/heads/{info['default_branch']}"))
            conn.commit()
            print(f"  Created local mirror of '{repo_name}'")

        if (info["latest_rev"] or 0) <= local_rev:
            print("Already up to date.")
            return 0

        commits = _fetch_json(f"{remote_url}/api/sync/{repo_name}/commits?since_rev={local_rev}")
        
        for c in commits:
            print(f"Pulling commit {c['rev']}: {c['commit_hash'][:8]}")
            
            if not _blob_exists_local(repo_root, c["tree_hash"]):
                b = _fetch_blob(f"{remote_url}/api/sync/{repo_name}/blob/{c['tree_hash']}")
                objects.store_blob(b, objects_dir)

            for cs in c["changesets"]:
                h = cs["blob_after"]
                if h and not _blob_exists_local(repo_root, h):
                    b = _fetch_blob(f"{remote_url}/api/sync/{repo_name}/blob/{h}")
                    objects.store_blob(b, objects_dir)

            parent_hashes = c.get("parent_hashes")
            db.execute(conn, """
                INSERT INTO repo_commits (commit_hash, repo_id, tree_hash, author_id, author_name, committer_id, committer_name, message, parent_hashes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (c["commit_hash"], config["repo_id"], c["tree_hash"], config.get("user_id"), c["author_name"], config.get("user_id"), c["committer_name"], c["message"], parent_hashes), commit=False)

            for cs in c["changesets"]:
                db.execute(conn, """
                    INSERT INTO repo_changesets (commit_hash, path, change_type, blob_before, blob_after, lines_added, lines_removed)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (c["commit_hash"], cs["path"], cs["change_type"], cs["blob_before"], cs["blob_after"], cs.get("lines_added", 0), cs.get("lines_removed", 0)), commit=False)

            ref_name = f"refs/heads/{info['default_branch']}"
            db.execute(conn, """
                UPDATE repo_refs SET commit_hash = %s, updated_at = NOW() WHERE repo_id = %s AND ref_name = %s
            """, (c["commit_hash"], config["repo_id"], ref_name), commit=False)

        conn.commit()
        print(f"Pulled {len(commits)} commit(s).")
        # Save last synced rev to config
        if commits:
            latest_pulled_rev = max((c.get("rev") or 0) for c in commits)
            if latest_pulled_rev:
                config["last_synced_rev"] = latest_pulled_rev
                config_path = os.path.join(repo_root, ".olympusrepo", "config.json")
                import json as _json
                with open(config_path, "w") as _f:
                    _json.dump(config, _f, indent=2)
        
        latest_tree_hash = commits[-1]["tree_hash"]
        tree_data = objects.retrieve_blob(latest_tree_hash, objects_dir)
        if tree_data:
            tree_entries = json_mod.loads(tree_data.decode("utf-8"))
            worktree.save_index(repo_root, tree_entries)
            for path, h in tree_entries.items():
                full_path = os.path.join(repo_root, path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                objects.retrieve_to_file(h, full_path, objects_dir)
        print("Working tree updated.")
        return 0
    except Exception as e:
        conn.rollback()
        print(f"ERROR: Pull failed: {e}")
        return 1
    finally:
        conn.close()


def cmd_offer(args):
    """Offer local commits to canonical for review."""
    repo_root = worktree.find_repo_root()
    if not repo_root:
        print("ERROR: Not in a repository.")
        return 1

    config = worktree.load_config(repo_root)
    remotes = config.get("remotes", {})
    if args.remote not in remotes:
        print(f"ERROR: Remote '{args.remote}' not found.")
        return 1

    remote_url = remotes[args.remote]["url"].rstrip("/")
    repo_name = config.get("repo_name")
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(repo_root, ".olympusrepo", "objects")
    )

    conn = db.connect()
    try:
        info = _fetch_json(f"{remote_url}/api/sync/{repo_name}/info")
        canonical_latest_rev = info["latest_rev"]

        commits = db.query(conn, "SELECT * FROM repo_commits WHERE repo_id = %s AND rev IS NULL ORDER BY committed_at ASC", (config["repo_id"],))
        if not commits:
            print("No new local commits to offer.")
            return 0

        print(f"Offering {len(commits)} commit(s) to {remote_url}...")

        changes = []
        blobs = {}
        for c in commits:
            changesets = db.query(conn, "SELECT * FROM repo_changesets WHERE commit_hash = %s", (c["commit_hash"],))
            for cs in changesets:
                changes.append({
                    "path": cs["path"],
                    "change_type": cs["change_type"],
                    "blob_hash": cs["blob_after"],
                    "lines_added": cs["lines_added"],
                    "lines_removed": cs["lines_removed"]
                })
                if cs["blob_after"]:
                    b = objects.retrieve_blob(cs["blob_after"], objects_dir)
                    if b:
                        blobs[cs["blob_after"]] = base64.b64encode(b).decode("utf-8")

        payload = {
            "branch_name": worktree.get_current_branch(repo_root),
            "from_rev": None,
            "base_rev": canonical_latest_rev,
            "offered_by": config.get("user", "unknown"),
            "message": args.message or f"Offer from {config.get('user', 'unknown')}",
            "changes": changes,
            "blobs": blobs
        }

        resp = _post_json(f"{remote_url}/api/sync/{repo_name}/offer", payload)
        print(f"Offer received! Staging ID: {resp.get('staging_id')}")
        print(resp.get("message", ""))
        return 0
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode()
        except Exception:
            msg = ""
        print(f"ERROR: Offer rejected: {e.code} {e.reason} - {msg}")
        return 1
    except Exception as e:
        print(f"ERROR: Offer failed: {e}")
        return 1
    finally:
        conn.close()


def cmd_delete_repo(args):
    """Delete a repository and all its data."""
    conn = db.connect()
    try:
        requested_user = args.user or os.getenv("USER", "zeus")
        user = db.get_user_by_name(conn, requested_user)
        if not user or user["role"] != "zeus":
            print("ERROR: Only Zeus can delete repositories.")
            return 1

        r = repo.get_repo(conn, args.name)
        if not r:
            print(f"ERROR: Repository '{args.name}' not found.")
            return 1

        if not args.force:
            confirm = input(f"Delete '{args.name}'? This cannot be undone. Type the repo name to confirm: ")
            if confirm != args.name:
                print("Cancelled.")
                return 0

        try:
            # Delete in correct order respecting FK constraints
            conn.autocommit = False
            cur = conn.cursor()
            repo_id = r["repo_id"]
            cur.execute("DELETE FROM repo_audit_log WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_file_revisions WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_messages WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_staging_changes WHERE staging_id IN (SELECT staging_id FROM repo_staging WHERE repo_id = %s)", (repo_id,))
            cur.execute("DELETE FROM repo_staging WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_promotions WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_changesets WHERE commit_hash IN (SELECT commit_hash FROM repo_commits WHERE repo_id = %s)", (repo_id,))
            cur.execute("DELETE FROM repo_refs WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_commits WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_permissions WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_access WHERE repo_id = %s", (repo_id,))
            cur.execute("DELETE FROM repo_repositories WHERE repo_id = %s", (repo_id,))
            conn.commit()
            print(f"Repository '{args.name}' deleted.")
            return 0
        except Exception as e:
            conn.rollback()
            print(f"ERROR: {e}")
            return 1
    finally:
        conn.close()


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

    # remote
    p = sub.add_parser("remote", help="Manage remote instances")
    p.add_argument("remote_action", choices=["add","list","remove"])
    p.add_argument("remote_name", nargs="?")
    p.add_argument("url", nargs="?")
    p.add_argument("--role", default="canonical", choices=["canonical","mirror","fork"])

    # clone
    p = sub.add_parser("clone", help="Clone a repository from a server")
    p.add_argument("url", help="Repository URL")
    p.add_argument("dest", nargs="?", help="Destination directory (default: repo name)")
    p.add_argument("--branch", default="main")
    p.add_argument("--user", help="Your username")

    # pull
    p = sub.add_parser("pull", help="Pull commits from canonical")
    p.add_argument("--remote", default="origin")

    # offer
    p = sub.add_parser("offer", help="Offer local commits to canonical for review")
    p.add_argument("--remote", default="origin")
    p.add_argument("-m", "--message", help="Why this should be accepted")

    # delete-repo
    p = sub.add_parser("delete-repo", help="Delete a repository (Zeus only)")
    p.add_argument("name", help="Repository name")
    p.add_argument("--user", help="Zeus username")
    p.add_argument("--force", action="store_true", help="Skip confirmation")

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
        "remote": cmd_remote,
        "clone": cmd_clone,
        "pull": cmd_pull,
        "offer": cmd_offer,
        "delete-repo": cmd_delete_repo,
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
