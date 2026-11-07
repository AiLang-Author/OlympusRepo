# Security Audit — OlympusRepo

**Date:** 2026-11-07
**Base:** `aea0862`. Fixes developed in the RenkuOS fork ([AiLang-Author/RenkuOS-OlympusRepo](https://github.com/AiLang-Author/RenkuOS-OlympusRepo)) and backported here.
**Scope:** web app (`olympusrepo/web/*`), core library, SQL migrations, `setup.sh`, and the systemd unit.
**Not in scope:** the `relay/` service, the CLI's client-side checkout code (it already guards `..`), and third-party dependency CVEs (run `pip-audit` with network access).

Every *Fixed* item has a regression test (see [Verification](#verification)).

## Summary

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | Critical | Every install gets a `zeus` / `changeme` admin account | Fixed |
| 2 | Critical | Stored XSS in the notification dropdown → admin takeover | Fixed |
| 3 | High | Any logged-in user can read any private repo via `/api/sync/*` | Fixed |
| 4 | High | `/api/sync/{repo}/blob/{hash}` serves any repo's blobs, without login | Fixed |
| 5 | High | v2 issue API: anyone logged in can edit any issue, and post into private repos | Fixed |
| 6 | High | Federation offers accept any self-made key, with no rate limit or size cap | Fixed |
| 7 | High | Git smart-HTTP Basic auth is an unthrottled password oracle | Fixed |
| 8 | High | Repo paths not validated (`../`, `.git/hooks/…`) on upload / offer / tribute | Fixed |
| 9 | Medium | Sessions and PATs survive deactivation and password change | Fixed |
| 10 | Medium | Password-reset tokens stored in plaintext | Fixed |
| 11 | Medium | `X-Forwarded-For` trusts the hop the client controls | Fixed |
| 12 | Medium | No CSRF defence beyond SameSite; no security headers | Fixed |
| 13 | Medium | `prune` deletes blobs of pending offers (including anonymous offerings) | Fixed |
| 14 | Medium | `/zeus/config` shows the credential-encryption key | Fixed |
| 15 | Medium | Remote-credential encryption key stored next to the ciphertext | Fixed |
| 16 | Medium | Service runs as root on `0.0.0.0` over plain HTTP; `.env` world-readable | Fixed in template |
| 17 | Low | Import progress SSE is unauthenticated | Fixed |
| 18 | Low | Commit comments readable on private repos | Fixed |
| 19 | Low | Migration `010` runs before `011` creates its table and fails silently | Fixed (`020`) |
| 20 | Low | Assorted hardening gaps (see [Open items](#open-items)) | Open |

---

## Findings

### 1. Default admin account on every install — Critical
`sql/007_seed.sql` creates `zeus` with password `changeme`. `setup.sh` creates the real admin interactively, but then runs *every* `sql/0*.sql`, so the seed account is always created as well. **Fix:** `007_seed.sql` is now a no-op. `020_security.sql` disables any account whose password is still `changeme` and deletes its sessions.

### 2. Stored XSS in notifications — Critical
`templates/base.html` built each notification with `innerHTML = ... + n.message + ...`. Notification text contains issue titles, comment snippets, tribute reasons and federation-offer messages. Any account (signup is open) could file an issue titled `<img src=x onerror=…>` and assign it to Zeus. Unauthenticated attackers could do the same through a signed offer (see #6). Either way, script runs in Zeus's session.

**Fix:** messages are rendered with `textContent`, and `n.link` is only followed when it is a same-origin path (`/x`, not `//host` or `javascript:`). The CSP added in #12 is a second layer.

### 3. Private repos readable through the sync API — High
`/api/sync/{name}/info` and `/commits` only checked that *some* user was logged in when the repo was private. They never checked membership.

**Fix:** they now call `repo.check_visibility()` and return 404 when it fails.

### 4. Cross-repo blob disclosure — High
`/api/sync/{name}/blob/{hash}` served any blob in the shared object store. A hash taken from a private repo could be fetched through any public repo's URL, without logging in.

**Fix:** the blob must appear in `repo_changesets` for a commit of *that* repo. An index on `repo_changesets(blob_after)` keeps the lookup cheap.

### 5. v2 issue API authorization — High
- `PATCH /api/repos/{name}/issues/{n}`: any logged-in user could change any issue in any repo.
- Issue creation and comments had no visibility check, so outsiders could write into private repos.

**Fix:** every issue route now checks repo visibility (hidden repos return 404). Changing status, priority or assignee requires Zeus/Olympian, the repo owner, or write access. Inputs are validated, and assignees must be able to see the repo. (The RenkuOS fork replaces the issue system entirely with a Trac-style tracker.)

### 6. Unauthenticated federation offers — High
`POST /api/sync/{name}/offer` verified an Ed25519 signature, but accepted *any* key. Anyone could generate a key and create staging realms and Zeus notifications (the XSS vector in #2). The body was parsed before any size check. Blobs were written to disk before their hash was verified.

**Fix:**
- New `offer_policy` setting: `trusted` (the default), `open` or `closed`.
- New `offer_trusted_keys` setting: allowed signer keys, editable at `/zeus/config`.
- The body is capped before parsing, and each IP is limited to 20 offers per hour.
- Blob hashes are verified before anything is written.
- Change paths are validated and change types are checked.

**Upgrade note:** with the `trusted` default, existing Athens → Olympus federation stops working until Zeus adds each contributor's public key.

### 7. Git Basic-auth brute force — High
The smart-HTTP endpoints accepted password credentials with no throttling, which bypassed the web login limit (5 per 15 minutes). Each auth attempt also leaked a database connection.

**Fix:** after 10 failures per IP in 15 minutes the endpoint returns 429 (`OLYMPUSREPO_GIT_AUTH_MAX_FAILURES` changes the limit). The connection is now closed in a `finally` block.

### 8. Unvalidated repository paths — High
Web upload only stripped a leading `/`. Anonymous offers, tribute edits and federation offers stored paths as given. Paths like `../../x` or `src/.git/hooks/post-checkout` landed in history. The git bridge and other clients could then write them outside the work tree, or into `.git/hooks`.

**Fix:** `repo.normalize_repo_path()` rejects absolute paths, `.`/`..` segments, control characters and `.git` segments. It is applied on every path-accepting route.

### 9. Sessions outlive the account — Medium
`repo_validate_session` never checked `is_active`, and nothing revoked sessions or PATs. A deactivated user stayed logged in for up to 24 hours, and their PATs kept working.

**Fix:**
- `repo_validate_session` now joins `repo_users.is_active`.
- A trigger deletes sessions and revokes PATs on deactivation, and deletes sessions on password change.
- `verify_pat` checks `is_active`.

### 10. Plaintext reset tokens — Medium
Reset tokens were stored in plaintext.

**Fix:** only the SHA-256 digest is stored now. Tokens issued before the upgrade are invalidated.

### 11. Spoofable client IP — Medium
With `OLYMPUSREPO_TRUST_FORWARDED_FOR=1`, the app used the *left-most* `X-Forwarded-For` entry, which the client supplies. That let anyone bypass the anonymous-offer and auth rate limits.

**Fix:** the app now uses the right-most entry, the one the proxy appended. All rate limiters go through `_client_ip()`.

### 12. CSRF and security headers — Medium
The session cookie is `SameSite=Strict`, which blocks most CSRF, but there was no second layer.

**Fix:**
- State-changing requests that carry a session cookie must come from the app's own origin (`OLYMPUSREPO_TRUSTED_ORIGINS` adds extra origins). Git and offer clients carry no cookie and are unaffected.
- Every response now sets `Content-Security-Policy`, `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy`, plus HSTS when `OLYMPUSREPO_COOKIE_SECURE=1`.

### 13. `prune` deletes unreviewed contributions — Medium
`fsck.prune` counted a blob as referenced only if it appeared in a commit changeset or tree. Blobs of active staging realms and federation offers, including anonymous drive-by fixes, were deleted before anyone reviewed them.

**Fix:** the referenced set now includes staging changes, offer changes, `blob_before`, and ticket attachments.

### 14. Credential key shown in the UI — Medium
`/zeus/config` listed every `repo_server_config` row, including `git_creds_key`.

**Fix:** that key is excluded from the listing.

### 15. Credential encryption key stored with the data — Medium (fixed)
Upstream encrypted remote credentials with `pgp_sym_encrypt(..., (SELECT value FROM repo_server_config WHERE key='git_creds_key'))`. The key sat in the same database as the ciphertext, so any database dump or backup revealed every stored git token and SSH key. Passing a key to pgcrypto as a parameter would not have helped either: with `log_statement` enabled, Postgres logs bound parameters.

**Fix:** `olympusrepo/core/credbox.py`.
- **Encryption:** AES-256-GCM, done in Python. The key and the plaintext never reach PostgreSQL.
- **Key source:** `OLYMPUSREPO_CREDS_KEY` (64 hex characters), or `OLYMPUSREPO_CREDS_KEY_FILE`, or `<install>/.olympusrepo/creds.key`. The key file is created with mode 0600, and a group- or world-readable key file is refused.
- **Binding:** each ciphertext is bound to its remote (repo id + name), so moving it to another row fails authentication. A key id in the header turns a wrong key into a clear error instead of garbage output.
- **Migration:** at startup, the app re-encrypts legacy pgcrypto rows. It deletes `git_creds_key` only after every row verifies under the new key, all in one transaction. One bad row means nothing changes.
- **CLI:** `olympusrepo creds status | init | migrate | rotate`. `rotate` re-seals every row inside a transaction, then swaps the key file and keeps `creds.key.old` for recovery.
- `016_git_push.sql` no longer creates a key in the database.

**Operator notes:**
- Back up `creds.key` separately from the database. Without it, stored credentials have to be re-entered.
- Dumps taken *before* this upgrade still contain the old key next to the old ciphertexts. Revoke and reissue any tokens or deploy keys stored before upgrading.
- Rotation briefly mismatches a running server: requests that read credentials between the database commit and the key-file swap fail. Rotate during a quiet period.

### 16. Deployment — Medium
**Fix:**
- The template refuses root and uses `$SUDO_USER`.
- It binds `127.0.0.1` unless the installer opts into LAN access.
- `ReadWritePaths` is narrowed to `objects/ mirrors/ gateways/ .olympusrepo/`.
- It adds `SystemCallFilter=@system-service`, removes all capabilities, and sets `UMask=0077`.
- `setup.sh` writes `.env` with `umask 077`.

### 17–19. Smaller issues
- `/import/progress/{job_id}` now requires Zeus.
- `GET /api/repos/{name}/commit/{hash}/comments` now checks visibility.
- `010_fix_fk_cascades.sql` alters `repo_file_revisions` before `011` creates it. `setup.sh` runs migrations without `ON_ERROR_STOP`, so the statement failed silently on most installs. `020` re-applies it.

---

## Open items

| Item | Risk | Suggested fix |
|------|------|---------------|
| CSP needs `'unsafe-inline'` | XSS defence-in-depth is weaker | Move inline scripts and `onclick=` handlers to static JS with nonces |
| `HTTPException(500, str(e))` in several routes | Leaks SQL and internal details | Log server-side; return a generic message |
| Rate limiters live in process memory | Reset on restart; not shared across workers | Store counters in a Postgres table (same pattern as `repo_anon_rate_log`) |
| Import SSRF check is check-then-use | DNS rebinding or redirects can reach internal hosts (Zeus-only feature) | Pin the resolved IP, or set `http.followRedirects=false` |
| Zeus can import any local directory | Reads files the service user can read | Restrict to an allowlisted base dir |
| `StrictHostKeyChecking=accept-new` on SSH remotes | Trust on first use | Store a known_hosts entry per remote |
| Reset tokens printed to the journal | Readable by the `adm` group | Send by email; restrict journal access |
| Registration open by default | Spam and abuse | Set `registration_policy=invite_only` for public instances |
| `flash_pat` cookie is not HttpOnly (120 s) | A token is briefly readable by script | Pass it in a one-time server-side flash instead |
| WebSocket mana has no Origin check | SameSite=Strict mitigates it | Check `Origin` in `mana_websocket` |
| Dependencies not scanned | Unknown CVEs | `pip-audit -r docs/requirements.txt` in CI |

## Verification

The tests run against a throwaway PostgreSQL 16 cluster with all migrations applied:
- `test_security.py`: 30 checks, one or more per fixed finding, including the issue-route authorization.
- `test_credbox.py`: 20 checks covering legacy migration (including rollback on a corrupt row), a `pg_dump` with no plaintext or key, tamper and row-swap rejection, wrong-key and loose-permission errors, rotation, and migration at startup.
- A smoke test of the existing pages (home, repo, commits, mana, Zeus pages, tokens, remotes, web upload).

All passed on 2026-11-07.
