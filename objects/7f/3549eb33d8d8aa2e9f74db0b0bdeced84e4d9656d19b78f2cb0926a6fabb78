"""
olympusrepo/core/repo_setup.py
Post-clone and instance setup automation.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import os
import secrets
import socket
import json
from . import db, worktree


def ensure_local_user(conn, username: str = None) -> dict:
    """
    Find or create a local user account.
    Uses $USER env var if username not provided.
    Falls back to hostname if $USER not set.
    Creates with role='titan' and a random password if not found.
    Returns the full user dict.
    """
    username = username or os.getenv("USER") or os.getenv("USERNAME") or socket.gethostname()
    username = username.lower().strip()

    user = db.get_user_by_name(conn, username)
    if user:
        return user

    # Create with random password — local CLI use, password not important
    random_pw = secrets.token_urlsafe(16)
    try:
        user_id = db.create_user(conn, username, random_pw, role="titan")
        conn.commit()
        print(f"  Created local user '{username}' (titan)")
        return db.get_user(conn, user_id)
    except Exception as e:
        conn.rollback()
        # If creation failed, try get again (race condition)
        user = db.get_user_by_name(conn, username)
        if user:
            return user
        raise RuntimeError(f"Could not create local user '{username}': {e}")


def write_config_user(repo_root: str, user: dict):
    """
    Write user and user_id into .olympusrepo/config.json.
    Safe to call multiple times — only updates user fields.
    """
    config = worktree.load_config(repo_root)
    config["user"]    = user["username"]
    config["user_id"] = user["user_id"]
    config_path = os.path.join(repo_root, ".olympusrepo", "config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def ensure_origin_remote(repo_root: str, base_url: str):
    """
    Add 'origin' remote to config.json if not already present.
    Safe to call multiple times.
    """
    config = worktree.load_config(repo_root)
    remotes = config.setdefault("remotes", {})
    if "origin" not in remotes:
        remotes["origin"] = {"url": base_url.rstrip("/"), "role": "canonical"}
        config_path = os.path.join(repo_root, ".olympusrepo", "config.json")
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"  Added remote 'origin' → {base_url}")


def ensure_repo_record(conn, repo_info: dict):
    """
    Ensure the repo record exists in the local DB.
    Safe to call if record already exists (ON CONFLICT DO NOTHING).
    """
    db.execute(conn, """
        INSERT INTO repo_repositories
            (repo_id, name, visibility, owner_id,
             default_branch, description)
        VALUES (%s, %s, %s, 1, %s, %s)
        ON CONFLICT (repo_id) DO UPDATE SET
            name           = EXCLUDED.name,
            visibility     = EXCLUDED.visibility,
            default_branch = EXCLUDED.default_branch
    """, (repo_info["repo_id"], repo_info["repo_name"],
          repo_info["visibility"], repo_info["default_branch"],
          f"Clone of {repo_info.get('server_url', '')}"))

    db.execute(conn, """
        INSERT INTO repo_refs (repo_id, ref_name, updated_by)
        VALUES (%s, %s, 1)
        ON CONFLICT DO NOTHING
    """, (repo_info["repo_id"],
          f"refs/heads/{repo_info['default_branch']}"))
    conn.commit()


def post_clone_setup(repo_root: str, base_url: str, conn,
                     username: str = None):
    """
    Full post-clone setup in one call. Run this at the end of
    cmd_clone() to ensure the cloned repo is immediately usable.

    Does:
    1. Find or create local user account
    2. Write user + user_id to config.json
    3. Add origin remote pointing at base_url
    4. Print a clean summary

    Args:
        repo_root: path to the cloned repo directory
        base_url:  canonical instance URL (e.g. http://localhost:8000)
        conn:      active DB connection to local instance DB
        username:  override username (default: $USER env var)
    """
    print("  Setting up local environment...")

    user = ensure_local_user(conn, username)
    write_config_user(repo_root, user)
    ensure_origin_remote(repo_root, base_url)

    print(f"  Local user:  {user['username']} (id={user['user_id']})")
    print(f"  Remote:      origin → {base_url}")
    print(f"  Ready. cd {os.path.basename(repo_root)} && olympusrepo status")


def init_instance_user(conn, username: str = None) -> dict:
    """
    Called by setup.sh equivalent — ensure the instance has at
    least one zeus-level user matching the current system user.
    Different from post_clone_setup: creates with role='zeus'
    and prompts for password via CLI if interactive.
    """
    username = username or os.getenv("USER") or "zeus"
    user = db.get_user_by_name(conn, username)
    if user:
        return user

    import getpass
    print(f"\nNo local user '{username}' found.")
    while True:
        pw = getpass.getpass(f"  Set password for '{username}' (min 8 chars): ")
        if len(pw) >= 8:
            break
        print("  Password too short.")

    user_id = db.create_user(conn, username, pw, role="zeus")
    conn.commit()
    print(f"  Created Zeus account: {username}")
    return db.get_user(conn, user_id)