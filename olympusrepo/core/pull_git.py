"""
olympusrepo/core/pull_git.py
Incremental pull from a git remote. Maintains a persistent bare mirror
per remote so pulls are O(new commits), not O(full history).
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import os
import shutil
import subprocess
from . import git_remotes, import_git


def _ensure_mirror(conn, remote: dict, mirrors_root: str) -> str:
    """Ensure a bare mirror exists for this remote; return its path."""
    path = remote.get("mirror_path")
    if path and os.path.isdir(path) and os.path.isdir(os.path.join(path, "objects")):
        return path

    os.makedirs(mirrors_root, exist_ok=True)
    path = os.path.join(mirrors_root, f"remote_{remote['remote_id']}.git")

    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)

    url = git_remotes.build_authenticated_url(remote)
    subprocess.run(
        [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
         "clone", "--bare", "--quiet",
         "--no-tags", "--filter=blob:none",  # lazy blob fetch
         "--", url, path],
        check=True,
        env=import_git.GIT_ENV,
        timeout=import_git.GIT_TIMEOUT_SECONDS * 4,  # initial clone can be slow
        capture_output=True,
    )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repo_git_remotes SET mirror_path=%s WHERE remote_id=%s",
            (path, remote["remote_id"]),
        )
    return path


def pull_from_git(
    conn, *,
    repo_id: int,
    remote_name: str,
    branch: str,
    user_id: int,
    objects_dir: str,
    mirrors_root: str = None,
    progress_cb=None,
) -> dict:
    # Resolve mirrors_root: explicit arg wins, then OLYMPUSREPO_MIRRORS_DIR
    # env, then a "mirrors/" sibling of the repo root. The hardcoded
    # /var/lib/olympusrepo/mirrors default broke for installs that aren't
    # running as root or under systemd's standard layout (e.g. WSL dev).
    if mirrors_root is None:
        mirrors_root = os.environ.get(
            "OLYMPUSREPO_MIRRORS_DIR",
            os.path.join(os.path.dirname(__file__), "..", "..", "mirrors"),
        )
    """
    Fetch new commits from the remote and import any whose SHA isn't
    already in repo_commits.
    """
    remote = git_remotes.get_remote(conn, repo_id, remote_name)
    if not remote:
        raise ValueError(f"Remote not found: {remote_name}")

    mirror = _ensure_mirror(conn, remote, mirrors_root)

    # Fetch refs
    url = git_remotes.build_authenticated_url(remote)
    subprocess.run(
        [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
         "fetch", "--quiet", "--prune", "--no-tags",
         "--", url,
         f"+refs/heads/{branch}:refs/heads/{branch}"],
        check=True, cwd=mirror,
        env=import_git.GIT_ENV,
        timeout=import_git.GIT_TIMEOUT_SECONDS,
        capture_output=True,
    )

    # Previous local tip for this branch
    with conn.cursor() as cur:
        cur.execute("""
            SELECT commit_hash FROM repo_refs
            WHERE repo_id=%s AND ref_name=%s
        """, (repo_id, f"refs/heads/{branch}"))
        row = cur.fetchone()
    from_sha = row[0] if row else None

    # Open pull_log
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO repo_git_pull_log
                (repo_id, remote_id, ref_name, from_sha, status, started_by)
            VALUES (%s, %s, %s, %s, 'running', %s)
            RETURNING pull_id
        """, (repo_id, remote["remote_id"],
              f"refs/heads/{branch}", from_sha, user_id))
        pull_id = cur.fetchone()[0]

    try:
        # Get every commit on the branch in topo order. Filter out
        # those we already have.
        commits = import_git._get_commits(mirror, branch)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT commit_hash FROM repo_commits WHERE repo_id=%s",
                (repo_id,),
            )
            existing = {r[0] for r in cur.fetchall()}
        new_commits = [c for c in commits if c["sha"] not in existing]

        if not new_commits:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE repo_git_pull_log SET status='success',
                        to_sha=from_sha, commits_fetched=0, finished_at=NOW()
                    WHERE pull_id=%s
                """, (pull_id,))
            return {"commits_fetched": 0, "message": "already up to date"}

        # Reuse import_git's cat-file batch + commit writer
        batch = import_git._CatFileBatch(mirror)
        try:
            from . import repo as repo_mod
            for i, c in enumerate(new_commits):
                if progress_cb:
                    progress_cb(i + 1, len(new_commits), c["sha"][:8])
                paths = import_git._list_tree(mirror, c["sha"])
                files = []
                for path in paths:
                    blob = batch.read_blob(c["sha"], path)
                    if blob is not None:
                        files.append((path, blob))
                repo_mod.import_commit_row(
                    conn,
                    repo_id=repo_id,
                    commit_hash=c["sha"],
                    tree_hash=c["tree"],
                    parent_hashes=c["parents"],
                    author_name=c["author_name"],
                    author_email=c["author_email"],
                    authored_at_epoch=c["author_time"],
                    author_tz_offset=c.get("author_tz"),
                    committer_name=c["committer_name"],
                    committer_email=c["committer_email"],
                    committed_at_epoch=c["committer_time"],
                    committer_tz_offset=c.get("committer_tz"),
                    message=c["message"],
                    files=files,
                    objects_dir=objects_dir,
                )
        finally:
            batch.close()

        tip_sha = new_commits[-1]["sha"]
        repo_mod.set_ref(
            conn, repo_id=repo_id,
            ref_name=f"refs/heads/{branch}",
            commit_hash=tip_sha, user_id=user_id,
        )

        # Pre-populate commit map: for these just-pulled commits the
        # olympus commit_hash equals the git_sha on this remote.
        with conn.cursor() as cur:
            for c in new_commits:
                cur.execute("""
                    INSERT INTO repo_git_commit_map
                        (olympus_commit_hash, remote_id, git_sha)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (c["sha"], remote["remote_id"], c["sha"]))
            cur.execute("""
                UPDATE repo_git_pull_log
                SET status='success', to_sha=%s, commits_fetched=%s,
                    finished_at=NOW()
                WHERE pull_id=%s
            """, (tip_sha, len(new_commits), pull_id))
            cur.execute("""
                UPDATE repo_git_remotes SET last_pull_at=NOW()
                WHERE remote_id=%s
            """, (remote["remote_id"],))

        return {
            "pull_id":         pull_id,
            "commits_fetched": len(new_commits),
            "from_sha":        from_sha,
            "to_sha":          tip_sha,
        }

    except Exception as e:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE repo_git_pull_log
                SET status='failed', error_message=%s, finished_at=NOW()
                WHERE pull_id=%s
            """, (str(e)[:2000], pull_id))
        raise
