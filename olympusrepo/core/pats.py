"""
olympusrepo/core/pats.py
Personal Access Tokens for git CLI and API authentication.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import secrets
from datetime import datetime, timedelta, timezone

# Tokens are generated with the "olyp_" prefix so operators can grep
# them out of logs and chat pastes. Same pattern as GitHub's ghp_,
# Slack's xoxb-, etc. Keep this short — it's also the index key.
TOKEN_PREFIX = "olyp_"
PREFIX_LEN = 12  # "olyp_" + 7 random chars


def _generate_raw_token() -> str:
    # token_urlsafe(32) gives ~43 chars of base64url. Combined with the
    # prefix we have ~48 chars total, ~256 bits of entropy.
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def create_pat(
    conn, *,
    user_id: int,
    name: str,
    scopes: list[str] = None,
    expires_days: int | None = 365,
) -> dict:
    """
    Mint a new PAT. Returns the raw token exactly once — it is NOT
    stored and cannot be recovered. If the user loses it they must
    revoke and mint a new one.
    """
    if not name or len(name) > 64:
        raise ValueError("PAT name must be 1–64 characters")
    valid_scopes = {"git:read", "git:write", "api:read", "api:write"}
    scopes = scopes or ["git:read", "git:write"]
    for s in scopes:
        if s not in valid_scopes:
            raise ValueError(f"unknown scope: {s!r}")

    raw = _generate_raw_token()
    prefix = raw[:PREFIX_LEN]
    expires_at = None
    if expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO repo_pats
                (user_id, name, token_hash, token_prefix,
                 scopes, expires_at)
            VALUES
                (%s, %s,
                 crypt(%s, gen_salt('bf', 10)),
                 %s, %s, %s)
            RETURNING pat_id, created_at
        """, (user_id, name, raw, prefix, scopes, expires_at))
        pat_id, created_at = cur.fetchone()

    return {
        "pat_id":     pat_id,
        "name":       name,
        "token":      raw,           # shown to user once
        "prefix":     prefix,
        "scopes":     scopes,
        "expires_at": expires_at,
        "created_at": created_at,
    }


def verify_pat(conn, raw_token: str) -> dict | None:
    """
    Validate a raw PAT. Returns {user_id, pat_id, scopes} on success,
    None on any failure (bad prefix, expired, revoked, mismatch).

    Side effect on success: updates last_used_at.
    """
    if not raw_token or not raw_token.startswith(TOKEN_PREFIX):
        return None
    if len(raw_token) < PREFIX_LEN + 4:
        return None
    prefix = raw_token[:PREFIX_LEN]

    # Candidate rows are already filtered by the partial index on
    # (token_prefix) WHERE revoked_at IS NULL, so this is a quick lookup.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pat_id, user_id, scopes, expires_at
            FROM repo_pats
            WHERE token_prefix = %s
              AND revoked_at IS NULL
              AND token_hash = crypt(%s, token_hash)
        """, (prefix, raw_token))
        row = cur.fetchone()

    if not row:
        return None
    pat_id, user_id, scopes, expires_at = row
    if expires_at is not None and expires_at < datetime.now(timezone.utc):
        return None

    # Best-effort usage stamp. Not in a transaction with the verify
    # because write-every-request gets expensive; a lost stamp is fine.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE repo_pats SET last_used_at = NOW() WHERE pat_id = %s",
                (pat_id,),
            )
    except Exception:
        pass

    return {"user_id": user_id, "pat_id": pat_id, "scopes": scopes}


def list_pats(conn, user_id: int) -> list[dict]:
    """List PATs for a user. Does NOT include raw tokens (which are unrecoverable)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pat_id, name, token_prefix, scopes,
                   expires_at, last_used_at, created_at, revoked_at
            FROM repo_pats
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (user_id,))
        rows = cur.fetchall()
    cols = ("pat_id", "name", "prefix", "scopes",
            "expires_at", "last_used_at", "created_at", "revoked_at")
    return [dict(zip(cols, r)) for r in rows]


def revoke_pat(conn, *, user_id: int, pat_id: int) -> bool:
    """Mark a PAT as revoked. Returns True if it was active."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE repo_pats
            SET revoked_at = NOW()
            WHERE pat_id = %s AND user_id = %s AND revoked_at IS NULL
        """, (pat_id, user_id))
        return cur.rowcount > 0
