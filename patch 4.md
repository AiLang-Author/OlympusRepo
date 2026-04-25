# Phase 2 — Git Connector

Four files plus an amendment to `import_git.py`. Hand the whole thing to Claude Code / Gemini; the dependencies between files are called out inline.

Run order for the migration: **016 after 015.**

---

## `sql/016_git_push.sql`

```sql
-- sql/016_git_push.sql
-- Phase 2: Git remote push/pull support.
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- Adds:
--   * timezone offset columns on repo_commits (for SHA round-trip fidelity)
--   * repo_git_remotes: per-repo git remote configuration
--   * repo_git_commit_map: olympus_commit_hash <-> git_sha per remote
--   * repo_git_push_log / repo_git_pull_log: audit trail
--   * server-side credential encryption key in repo_server_config

BEGIN;

-- -------------------------------------------------------------------------
-- Timezone fidelity on commits
-- -------------------------------------------------------------------------
-- Git commit SHAs are computed over commit text that includes author
-- and committer lines like:
--     author Alice <a@x> 1700000000 +0200
-- The "+0200" is part of the hashed text. TIMESTAMPTZ normalizes to UTC
-- on read-back, so we need to store the offset string separately to
-- reconstruct byte-identical commit text at push time.
--
-- Format: exactly +HHMM or -HHMM (what git log --date=raw emits).
ALTER TABLE repo_commits
    ADD COLUMN IF NOT EXISTS author_tz_offset    TEXT,
    ADD COLUMN IF NOT EXISTS committer_tz_offset TEXT;

-- Commits that predate this migration have NULL offsets. Export code
-- falls back to "+0000" in that case, which will produce a different
-- SHA than the original. To reclaim SHA fidelity on older imports,
-- re-run the importer against the original git source.

-- -------------------------------------------------------------------------
-- Git remote configuration
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repo_git_remotes (
    remote_id       BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id)
                    ON DELETE CASCADE,
    name            TEXT NOT NULL,            -- e.g. 'origin', 'github'
    url             TEXT NOT NULL,            -- https://... or git@...
    auth_type       TEXT NOT NULL DEFAULT 'none'
                    CHECK (auth_type IN ('none','token','ssh_key')),
    -- Credentials are encrypted server-side with pgp_sym_encrypt using
    -- the master key stored in repo_server_config['git_creds_key'].
    -- Never write plaintext credentials here.
    auth_credential_enc  BYTEA,
    -- Cached bare mirror for pull/incremental push. Managed by the
    -- pull code; safe to delete, will be re-fetched on next pull.
    mirror_path     TEXT,
    last_push_at    TIMESTAMPTZ,
    last_pull_at    TIMESTAMPTZ,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      BIGINT REFERENCES repo_users(user_id),
    UNIQUE (repo_id, name)
);

CREATE INDEX IF NOT EXISTS idx_git_remotes_repo ON repo_git_remotes(repo_id);

-- -------------------------------------------------------------------------
-- Olympus commit -> git SHA mapping per remote
-- -------------------------------------------------------------------------
-- For commits imported from git: olympus_commit_hash == git_sha, and we
-- pre-populate on import (one row per known remote).
-- For native commits: git_sha is assigned on first push to each remote
-- and recorded here.
--
-- Per-remote keys because the same native commit pushed to two different
-- remotes will have identical git_sha (SHA is a function of commit text,
-- not destination), but tracking per-remote lets us answer "has this
-- commit been pushed to X?" without joining through push_log.
CREATE TABLE IF NOT EXISTS repo_git_commit_map (
    olympus_commit_hash TEXT NOT NULL
                        REFERENCES repo_commits(commit_hash) ON DELETE CASCADE,
    remote_id           BIGINT NOT NULL
                        REFERENCES repo_git_remotes(remote_id) ON DELETE CASCADE,
    git_sha             TEXT NOT NULL,
    pushed_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (olympus_commit_hash, remote_id)
);

CREATE INDEX IF NOT EXISTS idx_git_commit_map_remote
    ON repo_git_commit_map(remote_id, git_sha);

-- -------------------------------------------------------------------------
-- Push/pull audit
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repo_git_push_log (
    push_id         BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id)
                    ON DELETE CASCADE,
    remote_id       BIGINT REFERENCES repo_git_remotes(remote_id)
                    ON DELETE SET NULL,
    ref_name        TEXT NOT NULL,            -- e.g. 'refs/heads/main'
    from_sha        TEXT,                     -- remote's previous tip (NULL on initial)
    to_sha          TEXT,                     -- new tip after push
    commits_pushed  INTEGER DEFAULT 0,
    blobs_pushed    INTEGER DEFAULT 0,
    bytes_pushed    BIGINT DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','success','failed')),
    error_message   TEXT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    started_by      BIGINT REFERENCES repo_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_git_push_log_repo
    ON repo_git_push_log(repo_id, started_at DESC);

CREATE TABLE IF NOT EXISTS repo_git_pull_log (
    pull_id         BIGSERIAL PRIMARY KEY,
    repo_id         BIGINT NOT NULL REFERENCES repo_repositories(repo_id)
                    ON DELETE CASCADE,
    remote_id       BIGINT REFERENCES repo_git_remotes(remote_id)
                    ON DELETE SET NULL,
    ref_name        TEXT NOT NULL,
    from_sha        TEXT,                     -- local tip before pull
    to_sha          TEXT,                     -- local tip after pull
    commits_fetched INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','success','failed')),
    error_message   TEXT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    started_by      BIGINT REFERENCES repo_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_git_pull_log_repo
    ON repo_git_pull_log(repo_id, started_at DESC);

-- -------------------------------------------------------------------------
-- Bootstrap the credential-encryption key
-- -------------------------------------------------------------------------
-- Generated once on first run. All stored auth_credential_enc values
-- are encrypted with this key via pgp_sym_encrypt. If this key is lost
-- or rotated, all stored credentials must be re-entered.
--
-- The key lives in repo_server_config rather than in a file so that
-- backups of the database are self-contained. Operators who want
-- defense-in-depth should additionally encrypt the database at rest.
INSERT INTO repo_server_config (key, value)
SELECT 'git_creds_key', encode(gen_random_bytes(32), 'hex')
WHERE NOT EXISTS (
    SELECT 1 FROM repo_server_config WHERE key = 'git_creds_key'
);

COMMIT;
```

---

## `olympusrepo/core/git_remotes.py`

CRUD for git remote configuration. Kept deliberately small — the interesting logic is in push/pull.

```python
"""
olympusrepo/core/git_remotes.py
Manage git remote configuration (URL, auth credentials, mirror path).
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import re
from urllib.parse import urlparse, urlunparse, quote


# Remote names match git's own rules: letters, digits, ., _, -, /
# Starts with a letter or digit to avoid flag-lookalikes.
_REMOTE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9/_.\-]{0,63}$')

# URL scheme allowlist. We deliberately do not support file:// here even
# though git does — it's a common exfil vector in multi-tenant setups.
_ALLOWED_SCHEMES = ('https', 'http', 'ssh', 'git')


def _validate_remote_name(name: str) -> None:
    if not _REMOTE_NAME_RE.match(name):
        raise ValueError(f"Invalid remote name: {name!r}")


def _validate_remote_url(url: str) -> None:
    if url.startswith("-"):
        raise ValueError("Remote URL must not start with '-'")
    # git@host:path form -- treat as ssh, no urlparse
    if url.startswith("git@") or (":" in url and "://" not in url
                                  and "@" in url.split(":", 1)[0]):
        return
    p = urlparse(url)
    if p.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsupported URL scheme: {p.scheme!r}")
    if not p.hostname:
        raise ValueError("URL missing host")


def add_remote(
    conn, *,
    repo_id: int,
    name: str,
    url: str,
    user_id: int,
    auth_type: str = "none",
    credential: str | None = None,
) -> dict:
    """
    Register a git remote for a repo. `credential` is the raw token or
    SSH private key bytes; it's encrypted via pgp_sym_encrypt with the
    server's git_creds_key before storage.
    """
    _validate_remote_name(name)
    _validate_remote_url(url)
    if auth_type not in ("none", "token", "ssh_key"):
        raise ValueError(f"Invalid auth_type: {auth_type!r}")
    if auth_type != "none" and not credential:
        raise ValueError(f"auth_type={auth_type!r} requires a credential")

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO repo_git_remotes
                (repo_id, name, url, auth_type, auth_credential_enc, created_by)
            VALUES (
                %(repo_id)s, %(name)s, %(url)s, %(auth_type)s,
                CASE WHEN %(cred)s IS NULL THEN NULL
                     ELSE pgp_sym_encrypt(
                         %(cred)s,
                         (SELECT value FROM repo_server_config WHERE key='git_creds_key')
                     )
                END,
                %(user_id)s
            )
            RETURNING remote_id
        """, {
            "repo_id":   repo_id,
            "name":      name,
            "url":       url,
            "auth_type": auth_type,
            "cred":      credential,
            "user_id":   user_id,
        })
        remote_id = cur.fetchone()[0]
    return {"remote_id": remote_id, "name": name, "url": url}


def get_remote(conn, repo_id: int, name: str) -> dict | None:
    """Fetch remote config including decrypted credential (in-memory only)."""
    _validate_remote_name(name)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                remote_id, repo_id, name, url, auth_type,
                CASE WHEN auth_credential_enc IS NULL THEN NULL
                     ELSE pgp_sym_decrypt(
                         auth_credential_enc,
                         (SELECT value FROM repo_server_config WHERE key='git_creds_key')
                     )::TEXT
                END AS credential,
                mirror_path, last_push_at, last_pull_at, is_active
            FROM repo_git_remotes
            WHERE repo_id = %s AND name = %s
        """, (repo_id, name))
        row = cur.fetchone()
    if not row:
        return None
    cols = ("remote_id", "repo_id", "name", "url", "auth_type",
            "credential", "mirror_path", "last_push_at", "last_pull_at",
            "is_active")
    return dict(zip(cols, row))


def list_remotes(conn, repo_id: int) -> list[dict]:
    """List remotes for a repo WITHOUT decrypting credentials."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT remote_id, name, url, auth_type,
                   last_push_at, last_pull_at, is_active
            FROM repo_git_remotes
            WHERE repo_id = %s
            ORDER BY name
        """, (repo_id,))
        rows = cur.fetchall()
    cols = ("remote_id", "name", "url", "auth_type",
            "last_push_at", "last_pull_at", "is_active")
    return [dict(zip(cols, r)) for r in rows]


def delete_remote(conn, repo_id: int, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM repo_git_remotes WHERE repo_id=%s AND name=%s",
            (repo_id, name),
        )


def build_authenticated_url(remote: dict) -> str:
    """
    For token auth over HTTPS, rewrite the URL to embed the token as
    x-access-token:TOKEN@host. This is the GitHub/GitLab-standard form
    and avoids needing a credential helper subprocess.

    For ssh_key auth, returns the URL unchanged; caller is responsible
    for writing the key to a temp file and setting GIT_SSH_COMMAND.

    For 'none', returns the URL unchanged.
    """
    if remote["auth_type"] != "token" or not remote.get("credential"):
        return remote["url"]

    p = urlparse(remote["url"])
    if p.scheme not in ("https", "http"):
        # token auth only makes sense over HTTP(S)
        return remote["url"]

    token = quote(remote["credential"], safe="")
    netloc = f"x-access-token:{token}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
```

---

## `olympusrepo/core/export_git.py`

The core of Phase 2: push OlympusRepo history to a git remote via `fast-import`.

```python
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
from . import git_remotes


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


def _files_at_commit(conn, commit_hash: str) -> list[tuple[str, str]]:
    """
    Return [(path, blob_hash), ...] representing the full tree at a
    commit. For imported commits we stored full-tree snapshots as 'add'
    rows, so this query just reads them back. For native commits created
    by staging→promotion, we'd need to walk parent + apply delta; that
    logic belongs in core/repo.py as `materialize_tree(commit_hash)`.
    For now we try changesets and assume import-style full snapshots.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT path, blob_after
            FROM repo_changesets
            WHERE commit_hash = %s AND change_type IN ('add','modify')
              AND blob_after IS NOT NULL
            ORDER BY path
        """, (commit_hash,))
        return [(r[0], r[1]) for r in cur.fetchall()]


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
            conn, bare_repo, marks_file, commits,
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
    conn, bare_repo: str, marks_file: str,
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
            tree = _files_at_commit(conn, c["commit_hash"])
            for path, blob_hash in tree:
                if blob_hash in blob_mark_for:
                    continue
                content = objects_mod.read_blob(objects_dir, blob_hash)
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
        fi.stdin.close()
        _, err = fi.communicate(timeout=GIT_TIMEOUT)
        if fi.returncode != 0:
            raise RuntimeError(
                f"git fast-import failed ({fi.returncode}): "
                f"{err.decode('utf-8', errors='replace')[:500]}"
            )
    except Exception:
        try:
            fi.kill()
        except Exception:
            pass
        raise

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
```

---

## `olympusrepo/core/pull_git.py`

Incremental pull from a git remote. Reuses the import machinery; the key difference is that it maintains a persistent bare mirror so each pull is `git fetch` instead of `git clone`.

```python
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
    mirrors_root: str = "/var/lib/olympusrepo/mirrors",
    progress_cb=None,
) -> dict:
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
```

---

## Amendment to `import_git.py` — capture tz offsets

Two changes so imports carry the data `export_git.py` needs for SHA round-trip.

**1. Commit format string — add `%ai` (author ISO date) and `%ci` (committer ISO date):**

```python
_COMMIT_FMT = (
    "%H%x1e%T%x1e%P%x1e"
    "%an%x1e%ae%x1e%at%x1e%ai%x1e"   # +%ai for author tz
    "%cn%x1e%ce%x1e%ct%x1e%ci%x1e"   # +%ci for committer tz
    "%B%x1f"
)
```

**2. In `_get_commits`, parse the offset out of the ISO string and include it in the dict:**

```python
import re as _re
_TZ_RE = _re.compile(r'([+\-]\d{4})\s*$')

def _tz_from_iso(iso: str) -> str:
    m = _TZ_RE.search(iso.strip())
    return m.group(1) if m else "+0000"

# inside the parser loop, after splitting parts:
(sha, tree, parents_str,
 an, ae, at, ai,
 cn, ce, ct, ci,
 message) = parts[:12]
# ...
commits.append({
    "sha": sha, "tree": tree, "parents": parents,
    "author_name": an, "author_email": ae,
    "author_time": int(at), "author_tz": _tz_from_iso(ai),
    "committer_name": cn, "committer_email": ce,
    "committer_time": int(ct), "committer_tz": _tz_from_iso(ci),
    "message": message,
})
```

**3. In the loop that calls `import_commit_row`, pass the offsets through:**

```python
repo_mod.import_commit_row(
    conn, repo_id=repo_id,
    commit_hash=c["sha"],
    tree_hash=c["tree"],
    parent_hashes=c["parents"],
    author_name=c["author_name"],
    author_email=c["author_email"],
    authored_at_epoch=c["author_time"],
    author_tz_offset=c["author_tz"],            # new
    committer_name=c["committer_name"],
    committer_email=c["committer_email"],
    committed_at_epoch=c["committer_time"],
    committer_tz_offset=c["committer_tz"],      # new
    message=c["message"],
    files=file_list,
    objects_dir=objects_dir,
)
```

**4. `import_commit_row` itself gets two more parameters threaded into the SQL function call:**

```python
# Update the SQL function call in repo.py:
cur.execute("""
    SELECT repo_insert_imported_commit(
        %(repo_id)s, %(commit_hash)s, %(tree_hash)s, %(parent_hashes)s,
        %(author_name)s, %(author_email)s, %(authored_at)s, %(author_tz)s,
        %(committer_name)s, %(committer_email)s, %(committed_at)s, %(committer_tz)s,
        %(message)s, NULL, NULL
    ) AS rev
""", {..., "author_tz": author_tz_offset, "committer_tz": committer_tz_offset})
```

And update `repo_insert_imported_commit` in the 016 migration (or an amended 015) to accept and store the tz columns. That's a mechanical change I'll leave to the agent — it's just adding two parameters and two column assignments to the INSERT.