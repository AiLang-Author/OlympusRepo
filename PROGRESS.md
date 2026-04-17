# OlympusRepo — Progress & Todo
*Last updated: April 17, 2026*

---

## What Works

### Core Infrastructure
- [x] PostgreSQL schema — 17 base tables + 11 migration files
- [x] Content-addressable object store (SHA-256, loose objects)
- [x] Full transaction safety — atomic commits, rollback on failure
- [x] Row-level security for private mana
- [x] bcrypt password hashing via pgcrypto
- [x] Session management with lazy expiry cleanup
- [x] FK cascade migrations (sql/010_fix_fk_cascades.sql)

### Authentication & Users
- [x] Login / logout with session cookies
- [x] Choose Your Fate landing screen
- [x] Browse Archives (public repos, no login required)
- [x] Sign up
- [x] Password change (account page)
- [x] Password reset via token (console output, no email yet)
- [x] Role system: zeus / olympian / titan / mortal / prometheus / hermes
- [x] User management page — create, role change, enable/disable
- [x] Account page — edit profile, change password

### Repositories
- [x] Create repo via web form (two-step wizard)
- [x] Repo browser — Files, Commits, Mana, Staging, Issues, Access, Settings tabs
- [x] Repo settings — description, visibility, delete
- [x] Repo access grants — grant/revoke per user
- [x] Repo permissions — promote, branch_create etc
- [x] File upload via drag-and-drop (single files working)
- [x] File tree loads from commit history
- [x] Binary file detection
- [x] File version history with timestamp navigation
- [x] Blob viewer with line numbers

### Commits & Branches
- [x] CLI commit flow — add, commit, status, log, diff
- [x] Web upload commit flow
- [x] Commit detail page
- [x] Commit comment threads
- [x] Branch create, switch, delete
- [x] File revision tracking (repo_file_revisions)
- [x] commit() prev_tree replay — correctly diffs index against full history

### Mana (Discussion)
- [x] Repo-level mana posts
- [x] Context-aware mana (file, commit, staging, general)
- [x] WebSocket live updates (requires uvicorn[standard])
- [x] Inline code comments on blob viewer
- [x] Mana threading (parent_id / thread_id)

### Messaging
- [x] Direct user-to-user messaging
- [x] Message threads with inline replies
- [x] Unread count badge in nav
- [x] Notification system with bell icon
- [x] Notification on DM received
- [x] Notification on issue assigned/commented

### Zeus Dashboard (The Throne)
- [x] Instance stats — repos, users, commits, promotions
- [x] Active staging realms
- [x] Audit log (last 20 entries)
- [x] Full audit log page with filters
- [x] Quick actions — manage users, server config, create repo

### Bug Tracker
- [x] Issue list with filters (status, type, priority, assignee)
- [x] Create issue — title, description, type, priority, assignee, file attachment
- [x] Issue detail — comments, linked files, linked commits, status/priority update
- [x] Auto-link commits to issues via "fixes #N" / "closes #N" in message
- [x] Auto-close issue when linked commit is "fixed"
- [x] Notifications on issue assign and comment

### Connector (v0.4 — FULLY CLOSED LOOP ✓)
- [x] `olympusrepo remote add/list/remove`
- [x] `olympusrepo pull` — sync commits from canonical to slave
- [x] `olympusrepo offer` — push staging realm to canonical for review
- [x] `olympusrepo clone` — full repo clone via sync protocol
- [x] Canonical exposes sync API — `/api/sync/{name}/info|commits|blob`
- [x] Offer receiver — creates staging realm on canonical from slave offer
- [x] Two instances proved talking to each other (Olympus ↔ Athens)
- [x] Side-by-side diff engine (diff_side_by_side in diff.py)
- [x] Staging review page with side-by-side diff UI
- [x] Promote route — `POST /api/repos/{name}/promote/{staging_id}`
- [x] **Full loop proven: clone → commit → offer → review → promote → canonical**
- [x] OlympusRepo source tracked in OlympusRepo (dogfood moment)
- [x] Empty blob handling fixed (zero-byte files store and serve correctly)
- [x] Clone writes files to working tree after fetch
- [x] rev IS NULL on slave commits — canonical assigns rev at promotion

### UI & Design
- [x] Dark theme throughout
- [x] 14 mythological background images (Grok-generated)
- [x] Role badges with color coding
- [x] 404 page (Hades)
- [x] 403 page (Access Denied)
- [x] Responsive nav with role badges, notifications, messages

---

## Known Bugs

- [ ] **`cmd_clone` doesn't auto-create local user** — user_id is NULL in config after clone, commit fails until manually fixed
- [ ] **`cmd_clone` doesn't auto-add origin remote** — must run `olympusrepo remote add origin <url>` manually after clone
- [ ] **`objects/` not in DEFAULT_IGNORE_PATTERNS** — blob store gets committed accidentally
- [ ] **Absolute paths in `olympusrepo add`** — silently adds as index entries then commits as deletes; should normalize or reject
- [ ] **`objects_dir` mismatch** — CLI stores blobs in `.olympusrepo/objects/`, server serves from `objects/` at repo root; must use `OLYMPUSREPO_OBJECTS_DIR` env var consistently
- [ ] **WebSocket requires uvicorn[standard]** — `pip install 'uvicorn[standard]'`
- [ ] **Notification polling too frequent** — fires every 60s, should be 5 minutes
- [ ] **Message notification not clearing on read** — notification badge stays after reading thread
- [ ] **Folder drag-and-drop** — `webkitGetAsEntry` JS not applied to repo_browser.html yet
- [ ] **Web UI repo delete** — cascade was manual; `sql/010_fix_fk_cascades.sql` must be run on existing DBs
- [ ] **Clone path prefix** — if repo was committed with absolute paths, files show as deleted after clone
- [ ] **Two-instance venv collision** — running two instances on same machine requires explicit venv activation; PATH picks wrong binary silently

---

## Todo — Next Session

### Immediate Fixes (before anything else)
1. **`cmd_clone` auto-setup** — after clone completes:
   - Prompt for username if not set
   - Create user in local DB if not exists
   - Write `user` and `user_id` to config.json
   - Auto-add `origin` remote pointing at clone source
2. **`OLYMPUSREPO_OBJECTS_DIR` env var** — make CLI and server both read from this; add to `setup.sh` and `.env`
3. **`objects/` in DEFAULT_IGNORE_PATTERNS** — one-line fix in `worktree.py`
4. **Absolute path normalization in `cmd_add`** — strip repo root prefix or reject with clear error
5. **Notification polling** — change 60000ms to 300000ms in base.html
6. **Message notification clear on read** — UPDATE repo_notifications in message_thread_page

### Setup Script Improvements
7. **`setup.sh` venv logic** — mandatory on WSL2, optional on native Linux
8. **`setup.sh` alias generation** — `olympus-canonical` and `olympus-athens` aliases written to shellrc
9. **`setup.sh` user creation** — create Zeus user in DB as part of install, no separate user-create step
10. **`setup.sh` first repo init** — optionally init starter repo after install
11. **`setup.sh` objects_dir config** — write `OLYMPUSREPO_OBJECTS_DIR` to `.env`

### Repair & Cleanup Utility
12. **`olympusrepo fsck`** — new CLI command:
    - Find commits with missing blobs
    - Find orphaned blob objects (not referenced by any commit)
    - Find changesets with NULL blob_after on non-delete entries
    - Report and optionally fix
13. **`olympusrepo prune`** — GC unreferenced blobs from object store

### Connector Hardening
14. **Test `olympusrepo pull`** — full pull flow from canonical to slave after new commits
15. **Test multi-file offer** — offer with 10+ files, verify all blobs transfer
16. **Test promote assigns rev** — verify promoted commit gets canonical rev, not NULL
17. **Conflict detection** — what happens when two people offer conflicting changes?

### CLI Pass
18. `olympusrepo mana` command — post and list mana from CLI
19. `olympusrepo issue` commands — new, list, close, assign
20. Test all 14 CLI commands end to end with clean install

### Build System (Phase 2 — after connector solid)
21. `sql/015_builds.sql` — repo_builds, repo_rev_tags, repo_file_faults
22. Build webhook endpoint
23. Rev tags in commits tab (BROKEN / STABLE / RELEASED badges)
24. Fault badges in file tree
25. Auto-issue on build failure
26. Build config per repo (enable/disable in repo settings)

### Mana Policy
27. Add `mana_policy` column to `repo_repositories`
28. Enforce in commit() and promote_staging()
29. Expose in repo settings UI

### Agora (Phase 3 — after relay)
30. Federated chat rooms — instance-to-instance via relay mesh
31. Channel ownership model (Zeus owns channels on their instance)
32. `#repo:name` auto-channels per repo
33. `#global:topic` federated channels spanning instances
34. Short-lived invite tokens for channel/instance joining

### OlympusRelay (Phase 3)
35. `relay/` directory — standalone `olympusrelay` service (~300 lines)
36. Instance identity — Ed25519 keypair generated on first run
37. Signed heartbeat registration
38. Gossip mesh between relay nodes
39. NAT hole-punch coordination
40. `olympus://instance_id/repo` URI scheme in CLI
41. Bootstrap relay list in `olympusrepo/relay_bootstrap.py`
42. Two community bootstrap relays running

### Quality of Life
43. Type-ahead user search (replace dropdowns)
44. User profile pages
45. Folder drag-and-drop upload
46. `sql/010_fix_fk_cascades.sql` — add to migration sequence so new installs get it automatically

### Docs
47. Update USER_MANUAL.md with connector workflow
48. Update SETUP.md with two-instance setup guide
49. Add CONNECTOR.md explaining master/slave model
50. Update README.md — feature list is outdated
51. Add RELAY.md (spec written, needs linking from README)

---

## Architecture Decisions Locked In

- **Master/Slave model** — canonical owns truth, slaves offer, never push
- **"Offer" not "push"** — language matters, enforced at protocol level
- **Rev is canonical** — rev numbers assigned by canonical at promotion, NULL on slave until promoted
- **Mana is permanent** — no delete, design rationale lives with the code
- **Roles are extensible** — CHECK constraint on text column, add roles via migration
- **Single Postgres instance** — no Redis, no Elasticsearch, no separate auth service
- **Content-addressable blobs** — idempotent, shared across repos, GC via prune
- **`OLYMPUSREPO_OBJECTS_DIR`** — single env var controls blob store location for both CLI and server
- **Identity = keypair** — relay identity is Ed25519 public key, not IP or domain

---

## Version Milestones

- **v0.1** — Design and schema (2 days)
- **v0.2** — Core build: CLI, web UI, auth, repos, mana, messaging, bug tracker (1 day)
- **v0.3** — Connector: clone, pull, offer, two-instance proof of concept
- **v0.4** — Full connector loop proven: clone → commit → offer → promote ✓ (today)
- **v0.5** — Setup script, repair utility, connector hardening, production ready
- **v0.6** — OlympusRelay: decentralized discovery, NAT hole-punch, `olympus://` URIs
- **v0.7** — Agora: federated chat, channels, invite tokens
- **v1.0** — AiLang connector, Claude review integration

---

## Session Notes — April 17, 2026



**Full connector loop closed today** after fixing:
- `commit()` prev_tree replay bug (was reading only last commit delta, not full history)
- `cmd_clone` not writing files to working tree after blob fetch
- Empty blob (zero-byte files) not stored or served correctly
- `rev IS NULL` design — slave commits have no rev until canonical promotes them
- `from_rev NOT NULL` constraint blocking offers
- Wrong venv executing wrong code (two-instance same-machine problem)
- `objects_dir` path mismatch between CLI and server

**Key insight:** `objects_dir` must be a single configured path shared by CLI and server. Currently CLI uses `.olympusrepo/objects/` and server uses `../../objects/` relative to app.py. This is the root cause of most blob 404 errors.

**Next push priorities:** fix `cmd_clone` auto-setup, `objects_dir` env var, `objects/` ignore pattern. Then `setup.sh` improvements. Then relay spec implementation.