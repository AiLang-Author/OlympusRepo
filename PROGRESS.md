# OlympusRepo — Progress & Todo
*Last updated: April 16, 2026*

---

## What Works

### Core Infrastructure
- [x] PostgreSQL schema — 17 base tables + 10 migration files
- [x] Content-addressable object store (SHA-256, loose objects)
- [x] Full transaction safety — atomic commits, rollback on failure
- [x] Row-level security for private mana
- [x] bcrypt password hashing via pgcrypto
- [x] Session management with lazy expiry cleanup

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
- [x] File upload via drag-and-drop (single files working, folder drag in progress)
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

### Connector (v0.3)
- [x] `olympusrepo remote add/list/remove`
- [x] `olympusrepo pull` — sync commits from canonical to slave
- [x] `olympusrepo offer` — push staging realm to canonical for review
- [x] `olympusrepo clone` — full repo clone via sync protocol
- [x] Canonical exposes sync API — `/api/sync/{name}/info|commits|blob`
- [x] Offer receiver — creates staging realm on canonical from slave offer
- [x] Two instances proved talking to each other (Olympus ↔ Athens)
- [x] Side-by-side diff engine (diff_side_by_side in diff.py)
- [x] Staging review page with side-by-side diff UI

### UI & Design
- [x] Dark theme throughout
- [x] 14 mythological background images (Grok-generated)
- [x] Role badges with color coding
- [x] 404 page (Hades)
- [x] 403 page (Access Denied)
- [x] Responsive nav with role badges, notifications, messages

---

## Known Bugs

- [ ] **Promotion route missing** — `POST /api/repos/{name}/promote/{staging_id}` not in app.py (Gemini patch pending)
- [ ] **WebSocket requires uvicorn[standard]** — `pip install 'uvicorn[standard]'`
- [ ] **Notification polling too frequent** — fires every 60s, should be 5 minutes
- [ ] **File index path mismatch after clone** — cloned files with `olympusrepo/` prefix show as deleted in status
- [ ] **Message notification not clearing on read** — notification badge stays after reading thread
- [ ] **`claude-` repo still in DB** — test artifact, needs cleanup
- [ ] **Folder drag-and-drop** — `webkitGetAsEntry` JS not applied to repo_browser.html yet
- [ ] **`_bump_file_revs` needs verification** — 231 rows exist but needs test after first commit

---

## Todo — Next Session

### Immediate Fixes (before anything else)
1. Apply promote route patch (Gemini has the code)
2. `pip install 'uvicorn[standard]'` for WebSocket
3. Fix notification polling interval — change 60000ms to 300000ms in base.html
4. Fix message notification clear on read
5. Apply folder drag-and-drop JS to repo_browser.html

### Connector Completion
6. Test full promote flow — Athens offer → Olympus staging → Zeus promotes → canonical updated
7. Fix clone path prefix issue (files cloned with parent folder name)
8. `olympusrepo pull` auto-create repo record (patch written, needs applying)
9. Add Review button to staging_detail.html → `/repo/{name}/staging/{id}/review`
10. Wire staging_review.html promote modal to the new promote route

### CLI Pass
11. Test all 14 CLI commands end to end
12. `olympusrepo mana` command
13. `olympusrepo issue` commands (new, list, close, assign)

### Setup Script
14. `setup.sh` — one-shot installer
    - Check prerequisites
    - Create DB user and database
    - Run migrations in order
    - Create venv and install
    - Prompt for zeus username and password (no default changeme)
    - Write .env file
    - Print next steps

### Build System (Phase 2 — after connector is solid)
15. `sql/015_builds.sql` — repo_builds, repo_rev_tags, repo_file_faults
16. Build webhook endpoint
17. Rev tags in commits tab (BROKEN / STABLE / RELEASED badges)
18. Fault badges in file tree
19. Auto-issue on build failure
20. Build config per repo (enable/disable in repo settings)

### Mana Policy
21. Add `mana_policy` column to `repo_repositories`
22. Enforce in commit() and promote_staging()
23. Expose in repo settings UI

### Quality of Life
24. Type-ahead user search (replace dropdowns in access/permissions/messaging)
25. Increase notification poll to 5 minutes
26. Add `objects/` to DEFAULT_IGNORE_PATTERNS (already fixed locally, needs commit)
27. `sql/010_fix_fk_cascades.sql` — ON DELETE CASCADE / SET NULL fixes
28. Delete repo CLI command fully tested
29. User profile pages

### Docs
30. Update USER_MANUAL.md with connector workflow
31. Update SETUP.md with slave instance setup
32. Add CONNECTOR.md explaining the master/slave model
33. Update README.md — current feature list is outdated

---

## Architecture Decisions Locked In

- **Master/Slave model** — canonical owns truth, slaves offer, never push
- **"Offer" not "push"** — language matters, enforced at protocol level  
- **Mana is permanent** — no delete, design rationale lives with the code
- **Roles are extensible** — CHECK constraint on text column, add roles via migration
- **Single Postgres instance** — no Redis, no Elasticsearch, no separate auth service
- **Content-addressable blobs** — idempotent, shared across repos, GC via prune
- **Timestamp-based file versioning** — human readable, no opaque rev numbers

---

## Version Milestones

- **v0.1** — Design and schema (2 days)
- **v0.2** — Core build: CLI, web UI, auth, repos, mana, messaging, bug tracker (1 day)
- **v0.3** — Connector: clone, pull, offer, two-instance proof of concept (tonight)
- **v0.4** — Build system integration, mana policy, setup script
- **v0.5** — Production hardening, federation between public instances
- **v1.0** — AiLang connector, Claude review integration
