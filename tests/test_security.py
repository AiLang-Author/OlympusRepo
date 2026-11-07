"""Regression tests for the RenkuOS security fixes. Run after test_tracker.py
import os as _os
if _os.environ.get("OLYMPUSREPO_DESTRUCTIVE_TESTS") != "1":
    raise SystemExit("Refusing to run: this test WIPES repo_users/repo_repositories. "
                     "Point OLYMPUSREPO_DB_* at a throwaway database and set OLYMPUSREPO_DESTRUCTIVE_TESTS=1.")

(reuses its users/repos: zeus2 owner, dev writer on 'haiku', outsider)."""
import base64, sys, hashlib
from fastapi.testclient import TestClient
from olympusrepo.core import db
from olympusrepo.web.app import app

conn = db.connect()
_c = conn.cursor()
_c.execute("TRUNCATE repo_users, repo_repositories RESTART IDENTITY CASCADE")
conn.commit()
_zeus = db.create_user(conn, "zeus2", "password123", "zeus")
_dev = db.create_user(conn, "dev", "password123", "mortal")
db.create_user(conn, "mortal", "password123", "mortal")
db.create_user(conn, "outsider", "password123", "mortal")
# the legacy default account, which 020_security.sql must disable
_c.execute("SELECT repo_create_user('zeus','changeme','zeus',NULL,NULL)")
_c.execute("INSERT INTO repo_repositories(name, owner_id, visibility) VALUES ('haiku', %s, 'public') RETURNING repo_id", (_zeus,))
_pub = _c.fetchone()[0]
_c.execute("INSERT INTO repo_repositories(name, owner_id, visibility) VALUES ('secret', %s, 'private')", (_zeus,))
_c.execute("INSERT INTO repo_access(repo_id, user_id, access_level, granted_by) VALUES (%s,%s,'write',%s)", (_pub, _dev, _zeus))
_c.execute("INSERT INTO repo_issues(repo_id, number, title, reported_by) VALUES (%s, 1, 'public bug', %s)", (_pub, _zeus))
_c.execute("INSERT INTO repo_issues(repo_id, number, title, reported_by) SELECT repo_id, 1, 'private bug', %s FROM repo_repositories WHERE name='secret'", (_zeus,))
conn.commit()
_c.execute(open("sql/020_security.sql").read())   # the migration under test
conn.commit()
res = []
def check(label, cond, extra=""):
    res.append(bool(cond)); print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {extra}"))

def client(user=None, ip="127.0.0.1"):
    c = TestClient(app, base_url="http://testserver", client=(ip, 50000))
    if user:
        r = c.post("/api/auth/login", data={"username": user, "password": "password123"})
        assert r.status_code == 200, r.text
    return c

O, Z, A = client("outsider"), client("zeus2"), client()

# Private repo content via sync API
r = O.get("/api/sync/secret/info");     check("sync info: outsider blocked from private", r.status_code == 404, r.status_code)
r = O.get("/api/sync/secret/commits");  check("sync commits: outsider blocked from private", r.status_code == 404, r.status_code)
r = Z.get("/api/sync/secret/info");     check("sync info: owner allowed", r.status_code == 200, r.status_code)

# Cross-repo blob disclosure: put a blob only in 'secret', request via public 'haiku'
from olympusrepo.core import objects as obj
import os
h = obj.store_blob(b"TOP SECRET", os.environ["OLYMPUSREPO_OBJECTS_DIR"])
cur = conn.cursor()
cur.execute("SELECT repo_id FROM repo_repositories WHERE name='secret'"); priv = cur.fetchone()[0]
cur.execute("INSERT INTO repo_commits(commit_hash,repo_id,tree_hash,author_name,committer_name,message) VALUES ('sec1',%s,'t','z','z','s')", (priv,))
cur.execute("INSERT INTO repo_changesets(commit_hash,path,change_type,blob_after) VALUES ('sec1','s.txt','add',%s)", (h,))
conn.commit()
r = A.get(f"/api/sync/haiku/blob/{h}")
check("sync blob: private blob not served via public repo", r.status_code == 404, (r.status_code, r.content[:20]))

# commit comments on private repo
r = O.get("/api/repos/secret/commit/sec1/comments")
check("commit comments: private repo hidden", r.status_code == 404, r.status_code)

# CSRF / origin check
r = O.post("/api/repos/haiku/issues", data={"title": "csrf"}, headers={"Origin": "https://evil.example"})
check("cross-origin POST with session refused", r.status_code == 403 and "Cross-origin" in r.text, (r.status_code, r.text[:80]))
r = O.post("/api/repos/haiku/issues", data={"title": "same origin ok"}, headers={"Origin": "http://testserver"})
check("same-origin POST allowed", r.status_code == 200, (r.status_code, r.text[:80]))

# Security headers
r = A.get("/login")
check("security headers present", r.headers.get("x-frame-options") == "DENY" and "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
      and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))

# Offer endpoint: untrusted signer refused
from olympusrepo.core import identity as idm
ident = idm.load_or_create(os.path.join(os.environ["HOME"], "ident"))
payload = {"branch_name": "x", "message": "<img src=x onerror=alert(1)>",
           "changes": [{"path": "a.txt", "change_type": "add", "blob_hash": hashlib.sha256(b"a").hexdigest()}],
           "blobs": {hashlib.sha256(b"a").hexdigest(): base64.b64encode(b"a").decode()}}
env = idm.sign_envelope(ident, payload) if hasattr(idm, "sign_envelope") else None
if env is None:
    print("SKIP offer tests (no sign_envelope helper)")
else:
    r = A.post("/api/sync/haiku/offer", json=env)
    check("offer from untrusted signer refused", r.status_code == 403, (r.status_code, r.text[:120]))
    payload["changes"][0]["path"] = "../../etc/cron.d/x"
    cur.execute("UPDATE repo_server_config SET value='open' WHERE key='offer_policy'"); conn.commit()
    r = A.post("/api/sync/haiku/offer", json=idm.sign_envelope(ident, payload))
    check("offer with traversal path refused", r.status_code == 400, (r.status_code, r.text[:120]))
    cur.execute("UPDATE repo_server_config SET value='trusted' WHERE key='offer_policy'"); conn.commit()
    r = A.post("/api/sync/haiku/offer", content=b"x" * (200 * 1024 * 1024), headers={"content-type": "application/json"})
    check("oversized offer body refused", r.status_code == 413, r.status_code)

# Upload path traversal (dev has write on haiku)
D = client("dev")
r = D.post("/api/repos/haiku/upload", data={"message": "m"}, files=[("files", ("../../outside.txt", b"x"))])
check("upload with ../ refused", r.status_code == 400, (r.status_code, r.text[:100]))
r = D.post("/api/repos/haiku/upload", data={"message": "m"}, files=[("files", ("src/.git/hooks/post-checkout", b"x"))])
check("upload into .git/ refused", r.status_code == 400, (r.status_code, r.text[:100]))

# old issue routes: authorization
r = O.post("/api/repos/secret/issues", data={"title": "sneaky"})
check("issues: outsider cannot file in private repo", r.status_code == 404, r.status_code)
r = O.post("/api/repos/secret/issues/1/comments", data={"content": "hi"})
check("issues: outsider cannot comment in private repo", r.status_code == 404, r.status_code)
r = O.patch("/api/repos/haiku/issues/1", data={"status": "closed"})
check("issues: random user cannot change a public issue", r.status_code == 403, r.status_code)
r = D.patch("/api/repos/haiku/issues/1", data={"status": "in_progress"})
check("issues: writer can change status", r.status_code == 200, (r.status_code, r.text[:100]))
r = Z.patch("/api/repos/haiku/issues/1", data={"priority": "bogus"})
check("issues: invalid priority rejected", r.status_code == 400, r.status_code)
r = O.post("/api/repos/haiku/issues", data={"title": "x", "file_path": "../../etc/passwd"})
check("issues: path traversal in file_path rejected", r.status_code == 400, r.status_code)
r = O.get("/repo/secret/issues/1")
check("issues: private issue page hidden", r.status_code in (403, 404), r.status_code)

# Sessions die when account deactivated / password changes
M = client("mortal")
check("mortal session works", M.get("/api/auth/me").status_code == 200)
cur.execute("UPDATE repo_users SET is_active=FALSE WHERE username='mortal'"); conn.commit()
check("deactivated user's session rejected", M.get("/api/auth/me").status_code == 401, M.get("/api/auth/me").status_code)
cur.execute("UPDATE repo_users SET is_active=TRUE WHERE username='mortal'"); conn.commit()

# Default-password account disabled by 021
cur.execute("SELECT is_active FROM repo_users WHERE username='zeus'"); row = cur.fetchone()
check("zeus/changeme account disabled", row is not None and row[0] is False, row)
r = client().post("/api/auth/login", data={"username": "zeus", "password": "changeme"})
check("zeus/changeme login fails", r.status_code == 401, r.status_code)

# Password reset tokens hashed at rest
r = client(ip="10.9.9.9").post("/api/auth/request-reset", data={"email": "nobody@example.com"})
cur.execute("UPDATE repo_users SET email='dev@example.com' WHERE username='dev'"); conn.commit()
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    client(ip="10.9.9.8").post("/api/auth/request-reset", data={"email": "dev@example.com"})
tok = buf.getvalue().split("token=")[-1].strip()
cur.execute("SELECT token, token_sha256 FROM repo_password_resets ORDER BY reset_id DESC LIMIT 1"); t, d = cur.fetchone()
check("reset token not stored in plaintext", t != tok and d == hashlib.sha256(tok.encode()).hexdigest(), (t[:8], tok[:8]))
r = client(ip="10.9.9.7").post("/api/auth/reset-password", data={"token": tok, "new_password": "newpassword1"})
check("reset with emailed token works", r.status_code == 200, r.text)

# Git smart-HTTP brute force throttled
G = TestClient(app, base_url="http://testserver", client=("10.1.1.1", 1))
codes = []
for i in range(12):
    codes.append(G.get("/secret.git/info/refs?service=git-upload-pack",
                       headers={"Authorization": "Basic " + base64.b64encode(b"zeus2:wrong%d" % i).decode()}).status_code)
check("git basic-auth brute force throttled (429 after 10)", codes[-1] == 429 and codes[0] == 401, codes)

# Import progress needs zeus
r = A.get("/import/progress/abc"); check("import SSE requires zeus", r.status_code == 403, r.status_code)

# config page hides git_creds_key
cur.execute("INSERT INTO repo_server_config(key,value) VALUES ('git_creds_key','SUPERSECRETKEY') ON CONFLICT (key) DO UPDATE SET value='SUPERSECRETKEY'"); conn.commit()
r = Z.get("/zeus/config"); check("config page hides creds key", r.status_code == 200 and "SUPERSECRETKEY" not in r.text, r.status_code)

# prune keeps blobs of pending staging offers
from olympusrepo.core import fsck
check("prune counts staging and offer blobs as referenced",
      "repo_staging_changes" in fsck._REFERENCED_SQL and "repo_offer_changes" in fsck._REFERENCED_SQL)

print(f"\n{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
