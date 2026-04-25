"""
olympusrepo/core/export_git.py
Push OlympusRepo history to a git remote via git fast-import.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License

Design:
  1. Determine which commits need pushing for the requested ref(s).
  2. Walk them in topological order (parents before children).
  3. Construct a fast-import stream: all needed blobs, then commits.
  4. Pipe the stream into `git fast-import` inside a temp bare repo.
  5. Read the marks file to learn the git SHA of each commit.
  6. `git push` from the temp repo to the configured remote URL.
  7. Record push log and update repo_git_commit_map.

The temp-repo approach is stateless and disposable. Re-fast-importing
full history each push is wasteful for large repos; a persistent
mirror could be layered on later using `from <sha>` anchors against
already-pushed commits. See TODO(persistent-mirror) below.

Imported commits that carry full original metadata (including
author_tz_offset / committer_tz_offset) should round-trip with their
original SHAs. A mismatch indicates metadata was lost somewhere.
"""

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from . import git_remotes, materialize as mat


GIT_BIN = shutil.which("git") or os.environ.get("OLYMPUSREPO_GIT_BIN") or "/usr/bin/git"

GIT_SAFE_ARGS = [
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "protocol.http.allow=always",
    "-c", "protocol.ssh.allow=always",
    "-c", "protocol.file.allow=user",
    "-c", "protocol.ext.allow=never",
]
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
GIT_TIMEOUT = int(os.environ.get("OLYMPUSREPO_GIT_TIMEOUT", "600"))


# --- fast-import stream framing -----------------------------------------
# See: https://git-scm.com/docs/git-fast-import
#
# Stream rules we rely on:
#   * "blob" command defines a blob; "mark :N" gives it a numeric id.
#   * "commit" command defines a commit; "from :N" links to parent.
#   * "M <mode> :N <path>" places blob N at path in this commit's tree.
#   * "D <path>" removes a path.
#   * "deleteall" clears the tree state (used for root commits).
#   * Marks are written to --export-marks=<file> as "<mark> <sha>\n".

def _fmt_author_line(kind: str, name: str, email: str,
                     ts_epoch: int, tz_offset: str | None) -> bytes:
    """
    kind is 'author' or 'committer'. tz_offset must be '+HHMM'/'-HHMM'
    or None (treated as +0000). Git is strict here; malformed offsets
    cause fast-import to abort.
    """
    tz = tz_offset if tz_offset and re.match(r'^[+\-]\d{4}$', tz_offset) else "+0000"
    # fast-import accepts the name as free-form but disallows LF and <>.
    # We sanitize here to avoid protocol breakage from pathological names.
    safe_name = name.replace("<", "").replace(">", "").replace("\n", " ").strip() or "Unknown"
    safe_email = email.replace("<", "").replace(">", "").replace("\n", "").strip() or "unknown@invalid"
    return f"{kind} {safe_name} <{safe_email}> {ts_epoch} {tz}\n".encode("utf-8")


def _emit_data(stream, payload: bytes) -> None:
    """Emit a fast-import 'data <N>\\n<bytes>' block."""
    stream.write(f"data {len(payload)}\n".encode("ascii"))
    stream.write(payload)
    stream.write(b"\n")


# --- commit graph walk --------------------------------------------------
def _commits_to_push(conn, repo_id: int, remote_id: int,
                     ref_commit_hash: str) -> list[dict]:
    """
    Return the list of commits reachable from `ref_commit_hash` that have
    NOT yet been pushed to the given remote, in topological order
    (parents before children).

    Walks the parent_hashes array starting at ref_commit_hash, pruning
    any subtree whose tip is already in repo_git_commit_map for this
    remote (that subtree is already on the remote, by induction).
    """
    # Already-pushed commits for this remote
    with conn.cursor() as cur:
        cur.execute("""
            SELECT olympus_commit_hash FROM repo_git_commit_map
            WHERE remote_id = %s
        """, (remote_id,))
        already_pushed = {row[0] for row in cur.fetchall()}

    # DFS from the ref tip, stopping at already-pushed ancestors
    needed: dict[str, dict] = {}
    stack = [ref_commit_hash]
    while stack:
        sha = stack.pop()
        if sha in needed or sha in already_pushed:
            continue
        with conn.cursor() as cur:
            cur.execute("""
                SELECT commit_hash, tree_hash, parent_hashes,
                       author_name, author_email, authored_at, author_tz_offset,
                       committer_name, committer_email, committed_at, committer_tz_offset,
                       message, is_imported
                FROM repo_commits
                WHERE commit_hash = %s AND repo_id = %s
            """, (sha, repo_id))
            row = cur.fetchone()
        if not row:
            # Dangling parent (shallow clone etc) -- skip silently.
            continue
        cols = ("commit_hash", "tree_hash", "parent_hashes",
                "author_name", "author_email", "authored_at", "author_tz_offset",
                "committer_name", "committer_email", "committed_at", "committer_tz_offset",
                "message", "is_imported")
        c = dict(zip(cols, row))
        needed[sha] = c
        for p in (c["parent_hashes"] or []):
            if p not in already_pushed:
                stack.append(p)

    # Topological sort: parents before children.
    ordered: list[dict] = []
    visited: set[str] = set()

    def visit(sha: str) -> None:
        if sha in visited or sha not in needed:
            return
        visited.add(sha)
        for p in (needed[sha]["parent_hashes"] or []):
            visit(p)
        ordered.append(needed[sha])

    for sha in needed:
        visit(sha)
    return ordered


def _files_at_commit(conn, repo_id: int, commit_hash: str) -> list[tuple[str, str]]:
    """
    Return [(path, blob_hash), ...] sorted by path, representing the
    full file tree at commit_hash.

    Uses materialize.materialize_tree so both imported commits (full
    snapshots) and native promotion commits (delta changesets) are
    handled correctly. Without this, a native commit pushed to a git
    remote would be missing files that existed before the commit but
    weren't touched by it.

    file_mode is intentionally dropped here; the fast-import emitter in
    push_to_git still hard-codes '100644' for Phase 2. When Phase 4
    lands and migration 017 adds the file_mode column, switch the
    emitter to call tree_for_export() instead, which returns the mode
    per-file.
    """
    try:
        tree = mat.materialize_tree(conn, repo_id, commit_hash)
    except ValueError:
        # commit_hash not found in this repo — shouldn't happen in
        # normal push flow, but return empty rather than crashing so
        # the caller can surface a clean error.
        return []
    return sorted(
        (path, blob_hash)
        for path, (blob_hash, _mode) in tree.items()
    )


# --- main entry point ---------------------------------------------------
def push_to_git(
    conn, *,
    repo_id: int,
    remote_name: str,
    ref_name: str,              # e.g. 'refs/heads/main'
    user_id: int,
    objects_dir: str,
    force: bool = False,
    progress_cb=None,
) -> dict:
    """
    Push the commits reachable from ref_name to the named git remote.
    """
    # Resolve ref -> commit
    with conn.cursor() as cur:
        cur.execute("""
            SELECT commit_hash FROM repo_refs
            WHERE repo_id = %s AND ref_name = %s
        """, (repo_id, ref_name))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"Ref not found: {ref_name}")
    tip_sha = row[0]

    remote = git_remotes.get_remote(conn, repo_id, remote_name)
    if not remote:
        raise ValueError(f"Remote not found: {remote_name}")
    if not remote.get("is_active"):
        raise ValueError(f"Remote {remote_name} is not active")

    commits = _commits_to_push(conn, repo_id, remote["remote_id"], tip_sha)
    if not commits:
        return {"commits_pushed": 0, "message": "already up to date"}

    # Open push_log row
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO repo_git_push_log
                (repo_id, remote_id, ref_name, to_sha, status, started_by)
            VALUES (%s, %s, %s, %s, 'running', %s)
            RETURNING push_id
        """, (repo_id, remote["remote_id"], ref_name, tip_sha, user_id))
        push_id = cur.fetchone()[0]

    tmp_dir = tempfile.mkdtemp(prefix="olympus_push_")
    bare_repo = os.path.join(tmp_dir, "repo.git")
    marks_file = os.path.join(tmp_dir, "marks.txt")
    try:
        subprocess.run(
            [GIT_BIN, *GIT_SAFE_ARGS, "init", "--bare", "--quiet", bare_repo],
            check=True, env=GIT_ENV, timeout=GIT_TIMEOUT,
            capture_output=True,
        )

        blobs_pushed, bytes_pushed = _stream_fast_import(
            conn, repo_id, bare_repo, marks_file, commits,
            objects_dir, ref_name, progress_cb,
        )

        # Read marks file: "<mark> <git_sha>\n" per line
        commit_marks: dict[int, str] = {}
        with open(marks_file, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) == 2 and parts[0].startswith(":"):
                    try:
                        commit_marks[int(parts[0][1:])] = parts[1]
                    except ValueError:
                        pass

        # Push
        push_url = git_remotes.build_authenticated_url(remote)
        push_args = [GIT_BIN, *GIT_SAFE_ARGS, "push"]
        if force:
            push_args.append("--force")
        push_args += ["--", push_url, f"{ref_name}:{ref_name}"]
        result = subprocess.run(
            push_args,
            cwd=bare_repo,
            check=False,
            capture_output=True,
            text=True,
            env=_ssh_env_for(remote, tmp_dir),
            timeout=GIT_TIMEOUT,
        )
        if result.returncode != 0:
            # Scrub any embedded token from error output before logging.
            err = _scrub_secrets(result.stderr, remote.get("credential"))
            _fail_push(conn, push_id, err)
            raise RuntimeError(f"git push failed: {err[:500]}")

        # Record the olympus->git SHA mapping. mark N corresponds to
        # commits[N-1] in emit order (we start marks at 1 for commits;
        # blobs get their own range starting at 10_000_000).
        with conn.cursor() as cur:
            for i, c in enumerate(commits, start=1):
                git_sha = commit_marks.get(i)
                if not git_sha:
                    continue
                cur.execute("""
                    INSERT INTO repo_git_commit_map
                        (olympus_commit_hash, remote_id, git_sha)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (olympus_commit_hash, remote_id)
                    DO UPDATE SET git_sha = EXCLUDED.git_sha, pushed_at = NOW()
                """, (c["commit_hash"], remote["remote_id"], git_sha))

            cur.execute("""
                UPDATE repo_git_push_log
                SET status='success',
                    commits_pushed=%s, blobs_pushed=%s, bytes_pushed=%s,
                    finished_at=NOW()
                WHERE push_id=%s
            """, (len(commits), blobs_pushed, bytes_pushed, push_id))
            cur.execute("""
                UPDATE repo_git_remotes SET last_push_at=NOW()
                WHERE remote_id=%s
            """, (remote["remote_id"],))

        return {
            "push_id":        push_id,
            "commits_pushed": len(commits),
            "blobs_pushed":   blobs_pushed,
            "bytes_pushed":   bytes_pushed,
            "tip_git_sha":    commit_marks.get(len(commits)),
        }

    except Exception as e:
        _fail_push(conn, push_id, str(e))
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- fast-import stream construction ------------------------------------
def _stream_fast_import(
    conn, repo_id: int, bare_repo: str, marks_file: str,
    commits: list[dict], objects_dir: str,
    ref_name: str, progress_cb,
) -> tuple[int, int]:
    """
    Spawn `git fast-import` and feed it the full stream. Returns
    (blobs_emitted, bytes_emitted). Commit marks are numbered 1..N in
    the order we emit commits; blob marks start at 10_000_000 so they
    can't collide with commit marks for the foreseeable future.
    """
    from . import objects as objects_mod

    fi = subprocess.Popen(
        [GIT_BIN, *GIT_SAFE_ARGS, "fast-import", "--quiet",
         f"--export-marks={marks_file}", "--done"],
        cwd=bare_repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=GIT_ENV,
    )

    blobs_emitted = 0
    bytes_emitted = 0
    blob_mark_for: dict[str, int] = {}  # blob_hash -> mark
    next_blob_mark = 10_000_000

    sha_to_commit_mark: dict[str, int] = {}

    try:
        for commit_idx, c in enumerate(commits, start=1):
            if progress_cb:
                progress_cb(commit_idx, len(commits), c["commit_hash"][:8])

            # First, emit any blobs this commit needs that we haven't
            # already emitted in this stream.
            tree = _files_at_commit(conn, repo_id, c["commit_hash"])
            for path, blob_hash in tree:
                if blob_hash in blob_mark_for:
                    continue
                content = objects_mod.retrieve_blob(blob_hash, objects_dir)
                fi.stdin.write(b"blob\n")
                fi.stdin.write(f"mark :{next_blob_mark}\n".encode("ascii"))
                _emit_data(fi.stdin, content)
                blob_mark_for[blob_hash] = next_blob_mark
                next_blob_mark += 1
                blobs_emitted += 1
                bytes_emitted += len(content)

            # Author / committer lines.
            fi.stdin.write(f"commit {ref_name}\n".encode("ascii"))
            fi.stdin.write(f"mark :{commit_idx}\n".encode("ascii"))
            sha_to_commit_mark[c["commit_hash"]] = commit_idx

            fi.stdin.write(_fmt_author_line(
                "author", c["author_name"], c["author_email"] or "",
                int(c["authored_at"].timestamp()) if c["authored_at"] else 0,
                c["author_tz_offset"],
            ))
            fi.stdin.write(_fmt_author_line(
                "committer", c["committer_name"], c["committer_email"] or "",
                int(c["committed_at"].timestamp()) if c["committed_at"] else 0,
                c["committer_tz_offset"],
            ))

            msg = (c["message"] or "").encode("utf-8")
            _emit_data(fi.stdin, msg)

            # Parents. Only include parents we're emitting in this
            # stream (if a parent is already on the remote, fast-import
            # can still reference it by full SHA, but we'd need to
            # verify presence first -- simpler to require the full
            # reachable set per push).
            parents = c["parent_hashes"] or []
            emitted_parents = [p for p in parents if p in sha_to_commit_mark]
            if not emitted_parents:
                fi.stdin.write(b"deleteall\n")
            else:
                fi.stdin.write(
                    f"from :{sha_to_commit_mark[emitted_parents[0]]}\n".encode("ascii")
                )
                for extra in emitted_parents[1:]:
                    fi.stdin.write(
                        f"merge :{sha_to_commit_mark[extra]}\n".encode("ascii")
                    )
                # For non-root commits we still re-emit the full tree
                # with `deleteall` + M lines. This is simpler than
                # computing deltas, and git's object dedup means it
                # costs nothing on the wire.
                fi.stdin.write(b"deleteall\n")

            # Tree state for this commit: M 100644 :mark path for each file.
            # Mode 100644 (regular non-executable file). Executable and
            # symlinks are a TODO — repo_changesets doesn't currently
            # carry file mode.
            for path, blob_hash in tree:
                mark = blob_mark_for[blob_hash]
                safe_path = path.replace("\n", "").lstrip("/")
                fi.stdin.write(
                    f"M 100644 :{mark} {safe_path}\n".encode("utf-8")
                )

            fi.stdin.write(b"\n")

        fi.stdin.write(b"done\n")
        fi.stdin.flush()
        fi.stdin.close()
        # NOTE: don't call fi.communicate() here — it tries to flush
        # stdin again and raises ValueError because we already closed
        # it. Use wait() + manual stderr drain instead.
        fi.wait(timeout=GIT_TIMEOUT)
        err = fi.stderr.read() if fi.stderr else b""
        if fi.returncode != 0:
            raise RuntimeError(
                f"git fast-import failed ({fi.returncode}): "
                f"{err.decode('utf-8', errors='replace')[:500]}"
            )
    except Exception as exc:
        # If fast-import died mid-stream we get e.g. ValueError on the
        # next stdin write/flush. Capture whatever it managed to print
        # before exiting so the operator sees the actual rejection
        # reason instead of a generic Python exception.
        stderr_msg = ""
        try:
            if fi.stderr:
                stderr_msg = fi.stderr.read(4096).decode(
                    "utf-8", errors="replace")
        except Exception:
            pass
        try:
            fi.kill()
        except Exception:
            pass
        raise RuntimeError(
            f"fast-import streaming aborted: {exc.__class__.__name__}: {exc}"
            + (f"\nfast-import stderr: {stderr_msg.strip()[:500]}"
               if stderr_msg.strip() else "")
        ) from exc

    return blobs_emitted, bytes_emitted


# --- helpers ------------------------------------------------------------
def _ssh_env_for(remote: dict, tmp_dir: str) -> dict:
    """
    If this remote uses ssh_key auth, write the key to a tempfile and
    set GIT_SSH_COMMAND to use it. Otherwise return GIT_ENV unchanged.
    """
    if remote.get("auth_type") != "ssh_key" or not remote.get("credential"):
        return GIT_ENV
    key_path = os.path.join(tmp_dir, "id_key")
    with open(key_path, "w") as f:
        f.write(remote["credential"])
    os.chmod(key_path, 0o600)
    return {
        **GIT_ENV,
        "GIT_SSH_COMMAND":
            f"ssh -i {key_path} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=accept-new -o BatchMode=yes",
    }


def _scrub_secrets(text: str, secret: str | None) -> str:
    if secret and secret in text:
        text = text.replace(secret, "***REDACTED***")
    # Also scrub token patterns commonly embedded in URLs.
    text = re.sub(r'x-access-token:[^@\s]+@', 'x-access-token:***@', text)
    return text


def _fail_push(conn, push_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE repo_git_push_log
            SET status='failed', error_message=%s, finished_at=NOW()
            WHERE push_id=%s
        """, (error[:2000], push_id))
