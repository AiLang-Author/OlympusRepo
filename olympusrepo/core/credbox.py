"""
olympusrepo/core/credbox.py
Encryption for stored git remote credentials.
MIT License

The key never enters PostgreSQL, so a database dump or backup alone cannot
reveal credentials. Key source, first match wins:

  1. OLYMPUSREPO_CREDS_KEY       64 hex chars (32 bytes)
  2. OLYMPUSREPO_CREDS_KEY_FILE  path to a file holding 64 hex chars
  3. <install>/.olympusrepo/creds.key   (created 0600 on first use)

Back the key up SEPARATELY from the database. Lose it and every stored
remote credential must be re-entered; leak it alongside a DB dump and the
credentials are exposed.

Ciphertext layout (stored in repo_git_remotes.auth_credential_enc):
  b"OLY1" | key_id (4 bytes = sha256(key)[:4]) | nonce (12) | AES-256-GCM ct+tag
The associated data binds a ciphertext to its remote, so a row's blob
cannot be copied onto another remote and decrypted there.

Legacy rows (pgcrypto pgp_sym_encrypt with repo_server_config.git_creds_key,
the upstream scheme) are converted by migrate_legacy(), which the web app
runs at startup and `olympusrepo creds migrate` runs by hand.
"""

from __future__ import annotations

import hashlib
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"OLY1"
_HEADER = len(MAGIC) + 4 + 12

DEFAULT_KEY_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", ".olympusrepo", "creds.key"))


class CredentialKeyError(RuntimeError):
    """Key missing, malformed, or not the key the data was sealed with."""


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------
def _parse_key(text: str, where: str) -> bytes:
    text = (text or "").strip()
    try:
        key = bytes.fromhex(text)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise CredentialKeyError(f"{where}: expected 64 hex characters (32 bytes).")
    return key


def key_file_path() -> str:
    return os.environ.get("OLYMPUSREPO_CREDS_KEY_FILE") or DEFAULT_KEY_FILE


def _write_key_file(path: str, key: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key.hex().encode() + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def load_key(create: bool = False) -> bytes:
    env = os.environ.get("OLYMPUSREPO_CREDS_KEY")
    if env:
        return _parse_key(env, "OLYMPUSREPO_CREDS_KEY")
    path = key_file_path()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        if not create:
            raise CredentialKeyError(
                f"No credential key: set OLYMPUSREPO_CREDS_KEY or create {path} "
                "(run `olympusrepo creds status`).")
        key = secrets.token_bytes(32)
        _write_key_file(path, key)
        print(f"  Credentials: generated new key at {path} — back it up separately from the database.")
        return key
    if st.st_mode & 0o077:
        raise CredentialKeyError(
            f"{path} is readable by group/others (mode {st.st_mode & 0o777:o}); chmod 600 it.")
    with open(path) as f:
        return _parse_key(f.read(), path)


def key_id(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()[:4]


# ---------------------------------------------------------------------------
# Seal / open
# ---------------------------------------------------------------------------
def _aad(repo_id: int, name: str) -> bytes:
    return f"repo_git_remotes:{int(repo_id)}:{name}".encode()


def is_sealed(blob) -> bool:
    return blob is not None and bytes(blob[:4]) == MAGIC


def seal(plaintext: str, repo_id: int, name: str, key: bytes | None = None) -> bytes:
    key = key or load_key(create=True)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _aad(repo_id, name))
    return MAGIC + key_id(key) + nonce + ct


def open_(blob, repo_id: int, name: str, key: bytes | None = None) -> str:
    blob = bytes(blob)
    if not is_sealed(blob) or len(blob) < _HEADER + 16:
        raise CredentialKeyError("Credential is not in OLY1 format (run `olympusrepo creds migrate`).")
    key = key or load_key()
    if blob[4:8] != key_id(key):
        raise CredentialKeyError(
            "Credential was sealed with a different key than the one configured "
            "(check OLYMPUSREPO_CREDS_KEY / key file).")
    try:
        return AESGCM(key).decrypt(blob[8:20], blob[20:], _aad(repo_id, name)).decode("utf-8")
    except Exception:
        raise CredentialKeyError("Credential failed authentication (tampered, or moved between remotes).")


# ---------------------------------------------------------------------------
# Migration / rotation
# ---------------------------------------------------------------------------
def status(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FILTER (WHERE substring(auth_credential_enc FROM 1 FOR 4) = %s),
                              COUNT(*) FILTER (WHERE substring(auth_credential_enc FROM 1 FOR 4) <> %s)
                         FROM repo_git_remotes WHERE auth_credential_enc IS NOT NULL""",
                    (MAGIC, MAGIC))
        sealed, legacy = cur.fetchone()
        cur.execute("SELECT EXISTS (SELECT 1 FROM repo_server_config WHERE key = 'git_creds_key')")
        db_key = cur.fetchone()[0]
    src = "env OLYMPUSREPO_CREDS_KEY" if os.environ.get("OLYMPUSREPO_CREDS_KEY") else key_file_path()
    try:
        load_key()
        key_ok, key_err = True, None
    except CredentialKeyError as e:
        key_ok, key_err = False, str(e)
    return {"sealed": sealed, "legacy": legacy, "db_key_present": db_key,
            "key_source": src, "key_ok": key_ok, "key_error": key_err}


def migrate_legacy(conn) -> int:
    """Re-encrypt pgcrypto rows under the external key, verify every row,
    then delete repo_server_config.git_creds_key. One transaction: either
    all rows convert and the DB key goes, or nothing changes."""
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM repo_server_config WHERE key = 'git_creds_key')")
        db_key = cur.fetchone()[0]
        cur.execute("""SELECT remote_id FROM repo_git_remotes
                        WHERE auth_credential_enc IS NOT NULL
                          AND substring(auth_credential_enc FROM 1 FOR 4) <> %s""", (MAGIC,))
        legacy_ids = [r[0] for r in cur.fetchall()]
    if not legacy_ids and not db_key:
        return 0
    if legacy_ids and not db_key:
        raise CredentialKeyError(
            f"{len(legacy_ids)} legacy credential(s) but git_creds_key is gone; "
            "those remotes must have their credentials re-entered.")

    key = load_key(create=True)
    try:
        with conn.cursor() as cur:
            for rid in legacy_ids:
                # Decrypt the legacy row inside Postgres with the legacy DB key
                # (that key is already in the DB, so nothing new is exposed).
                cur.execute("""
                    SELECT repo_id, name, pgp_sym_decrypt(auth_credential_enc,
                           (SELECT value FROM repo_server_config WHERE key = 'git_creds_key'))::TEXT
                      FROM repo_git_remotes WHERE remote_id = %s FOR UPDATE""", (rid,))
                repo_id, name, plain = cur.fetchone()
                cur.execute("UPDATE repo_git_remotes SET auth_credential_enc = %s WHERE remote_id = %s",
                            (seal(plain, repo_id, name, key), rid))
            _verify_all(cur, key)
            cur.execute("DELETE FROM repo_server_config WHERE key = 'git_creds_key'")
            cur.execute("""INSERT INTO repo_audit_log (action, target_type, details)
                           VALUES ('creds_migrate', 'config', %s::jsonb)""",
                        (f'{{"rows": {len(legacy_ids)}}}',))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(legacy_ids)


def _verify_all(cur, key: bytes) -> None:
    cur.execute("""SELECT repo_id, name, auth_credential_enc FROM repo_git_remotes
                    WHERE auth_credential_enc IS NOT NULL""")
    for repo_id, name, blob in cur.fetchall():
        open_(blob, repo_id, name, key)   # raises on any failure -> rollback


def rotate(conn) -> int:
    """Generate a new key, re-seal every credential, then swap the key file.
    The old key is kept as creds.key.old until you delete it."""
    if os.environ.get("OLYMPUSREPO_CREDS_KEY"):
        raise CredentialKeyError(
            "Key comes from OLYMPUSREPO_CREDS_KEY; rotate by re-sealing manually "
            "or switch to a key file first.")
    old = load_key()
    new = secrets.token_bytes(32)
    path = key_file_path()
    staged = path + ".new"
    _write_key_file(staged, new)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT remote_id, repo_id, name, auth_credential_enc FROM repo_git_remotes
                            WHERE auth_credential_enc IS NOT NULL FOR UPDATE""")
            rows = cur.fetchall()
            for rid, repo_id, name, blob in rows:
                plain = open_(blob, repo_id, name, old)
                cur.execute("UPDATE repo_git_remotes SET auth_credential_enc = %s WHERE remote_id = %s",
                            (seal(plain, repo_id, name, new), rid))
            _verify_all(cur, new)
        conn.commit()
    except Exception:
        conn.rollback()
        os.unlink(staged)
        raise
    os.replace(path, path + ".old")
    os.replace(staged, path)
    return len(rows)
