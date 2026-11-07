"""Credential-key tests: legacy migration, tamper/binding, rotation, atomicity.
Destructive — run only against a throwaway DB (see tests/README.md)."""
import os as _os
if _os.environ.get("OLYMPUSREPO_DESTRUCTIVE_TESTS") != "1":
    raise SystemExit("Refusing to run: set OLYMPUSREPO_DESTRUCTIVE_TESTS=1 against a throwaway DB.")

import os, subprocess, sys, tempfile
from olympusrepo.core import db, credbox, git_remotes

res = []
def check(label, cond, extra=""):
    res.append(bool(cond)); print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {extra}"))

keydir = tempfile.mkdtemp(prefix="credkeys-")
os.environ["OLYMPUSREPO_CREDS_KEY_FILE"] = os.path.join(keydir, "creds.key")
os.environ.pop("OLYMPUSREPO_CREDS_KEY", None)

conn = db.connect(); cur = conn.cursor()
cur.execute("TRUNCATE repo_users, repo_repositories RESTART IDENTITY CASCADE")
cur.execute("DELETE FROM repo_server_config WHERE key='git_creds_key'")
uid = db.create_user(conn, "owner", "password123", "zeus")
cur.execute("INSERT INTO repo_repositories(name, owner_id) VALUES ('r1', %s), ('r2', %s) RETURNING repo_id", (uid, uid))
r1 = cur.fetchone()[0]; r2 = r1 + 1
conn.commit()

# --- simulate an upstream install: DB-resident key + pgcrypto rows
cur.execute("INSERT INTO repo_server_config(key,value) VALUES ('git_creds_key', encode(gen_random_bytes(32),'hex'))")
for repo_id, name, cred in [(r1, "origin", "ghp_TOKEN_ONE"), (r1, "mirror", "ghp_TOKEN_TWO"), (r2, "origin", "-----BEGIN KEY-----")]:
    cur.execute("""INSERT INTO repo_git_remotes(repo_id,name,url,auth_type,auth_credential_enc,created_by)
                   VALUES (%s,%s,'https://example.com/x.git','token',
                           pgp_sym_encrypt(%s,(SELECT value FROM repo_server_config WHERE key='git_creds_key')),%s)""",
                (repo_id, name, cred, uid))
conn.commit()
st = credbox.status(conn)
check("status sees 3 legacy rows + DB key", st["legacy"] == 3 and st["db_key_present"], st)

# --- atomicity: one corrupt legacy row -> nothing changes
cur.execute("UPDATE repo_git_remotes SET auth_credential_enc = 'garbage'::bytea WHERE name='mirror'"); conn.commit()
try:
    credbox.migrate_legacy(conn); ok = False
except Exception:
    ok = True
st = credbox.status(conn)
check("corrupt row aborts migration, DB key kept, rows untouched", ok and st["legacy"] == 3 and st["db_key_present"], st)
cur.execute("""UPDATE repo_git_remotes SET auth_credential_enc =
               pgp_sym_encrypt('ghp_TOKEN_TWO',(SELECT value FROM repo_server_config WHERE key='git_creds_key'))
               WHERE name='mirror'"""); conn.commit()

# --- migrate
n = credbox.migrate_legacy(conn)
st = credbox.status(conn)
check("migrate converted 3 rows", n == 3 and st["sealed"] == 3 and st["legacy"] == 0, (n, st))
check("git_creds_key deleted from DB", not st["db_key_present"], st)
check("key file created 0600", oct(os.stat(os.environ["OLYMPUSREPO_CREDS_KEY_FILE"]).st_mode & 0o777) == "0o600")
check("get_remote decrypts", git_remotes.get_remote(conn, r1, "origin")["credential"] == "ghp_TOKEN_ONE")
check("ssh key round-trips", git_remotes.get_remote(conn, r2, "origin")["credential"] == "-----BEGIN KEY-----")
check("migrate is idempotent", credbox.migrate_legacy(conn) == 0)

# --- a DB dump alone reveals nothing
dump = subprocess.run(["pg_dump", "-h", os.environ["OLYMPUSREPO_DB_HOST"], "-p", os.environ["OLYMPUSREPO_DB_PORT"],
                       "-U", "olympus", "-d", "olympusrepo", "--data-only", "-t", "repo_git_remotes", "-t", "repo_server_config"],
                      capture_output=True, text=True, env={**os.environ, "PGPASSWORD": "testpw"}).stdout
key_hex = open(os.environ["OLYMPUSREPO_CREDS_KEY_FILE"]).read().strip()
check("pg_dump has no plaintext and no key", "ghp_TOKEN" not in dump and key_hex not in dump and "git_creds_key" not in dump,
      [l for l in dump.splitlines() if "ghp" in l or "git_creds_key" in l][:3])

# --- new remotes go straight to OLY1
git_remotes.add_remote(conn, repo_id=r2, name="gh", url="https://github.com/x/y.git", user_id=uid,
                       auth_type="token", credential="ghp_NEW"); conn.commit()
cur.execute("SELECT auth_credential_enc FROM repo_git_remotes WHERE name='gh'")
check("add_remote seals OLY1", bytes(cur.fetchone()[0][:4]) == b"OLY1")

# --- binding: copying a ciphertext to another remote fails
cur.execute("""UPDATE repo_git_remotes SET auth_credential_enc =
               (SELECT auth_credential_enc FROM repo_git_remotes WHERE repo_id=%s AND name='origin')
               WHERE repo_id=%s AND name='mirror'""", (r1, r1)); conn.commit()
try:
    git_remotes.get_remote(conn, r1, "mirror"); ok = False
except credbox.CredentialKeyError:
    ok = True
check("ciphertext moved between remotes is rejected", ok)
git_remotes.delete_remote(conn, r1, "mirror"); conn.commit()

# --- tamper
cur.execute("UPDATE repo_git_remotes SET auth_credential_enc = overlay(auth_credential_enc placing '\\xff'::bytea from 30 for 1) WHERE name='gh'"); conn.commit()
try:
    git_remotes.get_remote(conn, r2, "gh"); ok = False
except credbox.CredentialKeyError:
    ok = True
check("tampered ciphertext rejected", ok)
git_remotes.delete_remote(conn, r2, "gh"); conn.commit()

# --- wrong key -> clear error
os.environ["OLYMPUSREPO_CREDS_KEY"] = "11" * 32
try:
    git_remotes.get_remote(conn, r1, "origin"); msg = ""
except credbox.CredentialKeyError as e:
    msg = str(e)
check("wrong key gives clear error", "different key" in msg, msg)
os.environ.pop("OLYMPUSREPO_CREDS_KEY")

# --- loose permissions refused
os.chmod(os.environ["OLYMPUSREPO_CREDS_KEY_FILE"], 0o644)
try:
    credbox.load_key(); ok = False
except credbox.CredentialKeyError:
    ok = True
check("group/world-readable key file refused", ok)
os.chmod(os.environ["OLYMPUSREPO_CREDS_KEY_FILE"], 0o600)

# --- rotate
old_hex = open(os.environ["OLYMPUSREPO_CREDS_KEY_FILE"]).read().strip()
n = credbox.rotate(conn)
new_hex = open(os.environ["OLYMPUSREPO_CREDS_KEY_FILE"]).read().strip()
check("rotate re-sealed rows and swapped key", n == 2 and new_hex != old_hex
      and open(os.environ["OLYMPUSREPO_CREDS_KEY_FILE"] + ".old").read().strip() == old_hex, n)
check("credentials readable after rotate", git_remotes.get_remote(conn, r1, "origin")["credential"] == "ghp_TOKEN_ONE")
os.environ["OLYMPUSREPO_CREDS_KEY"] = old_hex
try:
    git_remotes.get_remote(conn, r1, "origin"); ok = False
except credbox.CredentialKeyError:
    ok = True
check("old key no longer opens rotated rows", ok)
os.environ.pop("OLYMPUSREPO_CREDS_KEY")

# --- app startup migrates automatically
cur.execute("INSERT INTO repo_server_config(key,value) VALUES ('git_creds_key', encode(gen_random_bytes(32),'hex'))")
cur.execute("""INSERT INTO repo_git_remotes(repo_id,name,url,auth_type,auth_credential_enc,created_by)
               VALUES (%s,'legacy','https://e.com/x.git','token',
                       pgp_sym_encrypt('ghp_LEGACY',(SELECT value FROM repo_server_config WHERE key='git_creds_key')),%s)""",
            (r1, uid)); conn.commit()
from fastapi.testclient import TestClient
from olympusrepo.web.app import app
with TestClient(app, client=("127.0.0.1", 1)) as c:
    c.get("/login")
st = credbox.status(conn)
check("web startup migrated legacy row and dropped DB key", st["legacy"] == 0 and not st["db_key_present"], st)
check("migrated legacy row decrypts", git_remotes.get_remote(conn, r1, "legacy")["credential"] == "ghp_LEGACY")

# --- CLI status exit code
cli = subprocess.run([sys.executable, "-m", "olympusrepo", "creds", "status"], capture_output=True, text=True, env=os.environ)
check("`olympusrepo creds status` healthy -> exit 0", cli.returncode == 0, cli.stdout + cli.stderr)

print(f"\n{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
