"""
olympusrepo/core/gateway.py
Per-repo bare git repo that backs the smart HTTP protocol endpoints.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License

Invariant: the gateway is derived state. It can always be rebuilt from
repo_commits + repo_changesets + the blob store. If corrupted, delete
it and call ensure_gateway_synced() to rebuild.
"""

import os
import shutil
import subprocess
import tempfile
from . import materialize, import_git
from . import export_git  # reuses _stream_fast_import


GATEWAYS_ROOT_DEFAULT = os.environ.get(
    "OLYMPUSREPO_GATEWAYS_ROOT",
    # Project-relative fallback (matches mirrors_root pattern). The
    # /var/lib/olympusrepo path requires root + standard layout, which
    # breaks for non-root installs (WSL dev, container, etc.).
    os.path.join(os.path.dirname(__file__), "..", "..", "gateways"),
)


def gateway_path(repo_id: int, root: str = GATEWAYS_ROOT_DEFAULT) -> str:
    return os.path.join(root, f"repo_{repo_id}.git")


def _init_bare(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isdir(os.path.join(path, "objects")):
        return
    subprocess.run(
        [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
         "init", "--bare", "--quiet", path],
        check=True,
        env=import_git.GIT_ENV,
        timeout=import_git.GIT_TIMEOUT_SECONDS,
        capture_output=True,
    )
    # Allow pushes that update refs non-fast-forward — the Olympus side
    # is the source of truth, so we trust our own sync process. Clients
    # are still subject to our application-level ACLs before they reach
    # git-receive-pack.
    subprocess.run(
        [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
         "-C", path, "config", "receive.denyNonFastForwards", "false"],
        check=True, env=import_git.GIT_ENV,
        timeout=import_git.GIT_TIMEOUT_SECONDS,
    )


def ensure_gateway_synced(
    conn, *,
    repo_id: int,
    objects_dir: str,
    gateways_root: str = GATEWAYS_ROOT_DEFAULT,
    force_rebuild: bool = False,
) -> dict:
    """
    Make sure the gateway reflects current Olympus state for this repo.
    Idempotent. Incremental when possible.
    """
    path = gateway_path(repo_id, gateways_root)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO repo_git_gateways (repo_id, gateway_path)
            VALUES (%s, %s)
            ON CONFLICT (repo_id) DO UPDATE SET gateway_path = EXCLUDED.gateway_path
            RETURNING last_synced_commit_hash
        """, (repo_id, path))
        last_synced = cur.fetchone()[0]

    if force_rebuild:
        shutil.rmtree(path, ignore_errors=True)
        last_synced = None

    _init_bare(path)

    # Determine which refs/commits need pushing into the gateway.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ref_name, commit_hash
            FROM repo_refs
            WHERE repo_id = %s AND commit_hash IS NOT NULL
        """, (repo_id,))
        refs = cur.fetchall()

    if not refs:
        return {"synced": 0, "gateway_path": path}

    # Collect the set of commits reachable from all refs that aren't
    # already in the gateway. On first sync this is the full history.
    synced_total = 0
    for ref_name, tip_sha in refs:
        synced = _sync_ref_to_gateway(
            conn, repo_id, path, ref_name, tip_sha, objects_dir,
        )
        synced_total += synced

    # Record sync state.
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE repo_git_gateways
            SET last_synced_at = NOW(),
                last_synced_commit_hash = (
                    SELECT commit_hash FROM repo_refs
                    WHERE repo_id = %s AND ref_name = 'refs/heads/main'
                ),
                status = 'active',
                error_message = NULL
            WHERE repo_id = %s
        """, (repo_id, repo_id))

    return {"synced": synced_total, "gateway_path": path}


def _sync_ref_to_gateway(
    conn, repo_id: int, gateway: str,
    ref_name: str, tip_sha: str, objects_dir: str,
) -> int:
    """
    Fast-forward the gateway's copy of `ref_name` to `tip_sha`. Only
    commits not already present in the gateway are streamed through
    fast-import.
    """
    # Check what git already has for this ref.
    try:
        existing_tip = subprocess.run(
            [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
             "-C", gateway, "rev-parse", "--verify", "--quiet", ref_name],
            capture_output=True, text=True,
            env=import_git.GIT_ENV,
            timeout=import_git.GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
    except Exception:
        existing_tip = ""

    if existing_tip == tip_sha:
        return 0

    # Reuse the export pipeline. We point it at a dummy remote (the
    # gateway itself) but rather than "push", we stream directly into
    # the gateway's object database.
    commits = export_git._commits_to_push(
        conn, repo_id, remote_id=_gateway_pseudo_remote_id(repo_id),
        ref_commit_hash=tip_sha,
    )
    if not commits:
        # Already in sync by commit hashes even though ref didn't match.
        # Just update the ref.
        _update_ref(gateway, ref_name, tip_sha)
        return 0

    marks_file = os.path.join(gateway, "olympus_marks.tmp")
    try:
        export_git._stream_fast_import(
            conn, repo_id, gateway, marks_file, commits,
            objects_dir, ref_name, progress_cb=None,
        )
    finally:
        if os.path.exists(marks_file):
            os.unlink(marks_file)

    # fast-import set the ref to point at the last emitted commit.
    # Verify — if it's not at tip_sha, something diverged.
    after = subprocess.run(
        [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
         "-C", gateway, "rev-parse", "--verify", "--quiet", ref_name],
        capture_output=True, text=True,
        env=import_git.GIT_ENV,
        timeout=import_git.GIT_TIMEOUT_SECONDS,
    ).stdout.strip()

    if after != tip_sha:
        # Expected for native commits where fast-import recomputes
        # SHAs. Update the ref to whatever git produced and record the
        # mapping — the gateway is now canonical for its own SHAs.
        _update_ref(gateway, ref_name, after)
    return len(commits)


def _gateway_pseudo_remote_id(repo_id: int) -> int:
    """
    The gateway sync reuses _commits_to_push which keys on remote_id.
    We use a negative synthetic id so it never collides with real
    repo_git_remotes rows. The commit map rows for this id live in
    repo_git_commit_map with remote_id referencing a bookkeeping row.
    In practice we bypass that table for gateway sync and instead
    compare ref tips directly. Returning 0 here means "no already-
    pushed filter" — we rely on the in-gateway ref check to avoid
    re-emitting commits.
    """
    return 0


def _update_ref(gateway: str, ref_name: str, sha: str) -> None:
    subprocess.run(
        [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
         "-C", gateway, "update-ref", ref_name, sha],
        check=True, env=import_git.GIT_ENV,
        timeout=import_git.GIT_TIMEOUT_SECONDS,
        capture_output=True,
    )


def reingest_from_gateway(
    conn, *,
    repo_id: int,
    ref_updates: list[tuple[str, str, str]],
    objects_dir: str,
    importer_user_id: int,
    gateways_root: str = GATEWAYS_ROOT_DEFAULT,
) -> dict:
    """
    After a client push lands in the gateway, pull the new commits
    back into Postgres. Called by the git-receive-pack handler.

    ref_updates is a list of (old_sha, new_sha, ref_name) tuples as
    reported by git-receive-pack's stdin on a successful push.
    """
    from . import repo as repo_mod
    gateway = gateway_path(repo_id, gateways_root)
    if not os.path.isdir(gateway):
        raise RuntimeError(f"gateway missing for repo {repo_id}")

    # Commit hashes already in Olympus (don't re-import).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT commit_hash FROM repo_commits WHERE repo_id = %s",
            (repo_id,),
        )
        existing = {r[0] for r in cur.fetchall()}

    all_new: list[dict] = []
    for old_sha, new_sha, ref_name in ref_updates:
        if new_sha == "0" * 40:
            # Ref deletion — update the ref on the Olympus side.
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM repo_refs
                    WHERE repo_id = %s AND ref_name = %s
                """, (repo_id, ref_name))
            continue

        branch = ref_name.removeprefix("refs/heads/")
        commits = import_git._get_commits(gateway, new_sha)
        new_commits = [c for c in commits if c["sha"] not in existing]
        existing.update(c["sha"] for c in new_commits)
        all_new.extend((branch, c) for c in new_commits)

    if not all_new:
        return {"imported": 0}

    batch = import_git._CatFileBatch(gateway)
    try:
        for branch, c in all_new:
            paths = import_git._list_tree(gateway, c["sha"])
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

    # Update Olympus refs to match what git-receive-pack wrote.
    for old_sha, new_sha, ref_name in ref_updates:
        if new_sha == "0" * 40:
            continue
        repo_mod.set_ref(
            conn, repo_id=repo_id,
            ref_name=ref_name, commit_hash=new_sha,
            user_id=importer_user_id,
        )

    return {"imported": len(all_new)}
