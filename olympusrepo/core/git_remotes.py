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
