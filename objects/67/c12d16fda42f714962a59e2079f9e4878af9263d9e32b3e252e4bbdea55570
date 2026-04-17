# OlympusRepo — Setup & CLI Validation Checklist
*2 Paws Machine and Engineering — April 2026*

---

## setup.sh

- [ ] Runs clean on a **fresh machine** with no prior OlympusRepo state
- [ ] Runs clean on a machine where **DB already exists** — handles gracefully, doesn't crash
- [ ] Runs clean on a machine where **venv already exists**
- [ ] Detects missing system deps and prints clear install instructions
  - [ ] `postgresql` / `postgresql-contrib`
  - [ ] `python3` (correct version)
  - [ ] `python3-venv`
  - [ ] `diffutils` (diff3)
- [ ] Creates DB user correctly — no hardcoded `changeme` password
- [ ] Prompts for zeus username and password
- [ ] Runs all `sql/0*.sql` migrations in order without error
- [ ] Writes `.env` file with correct values
- [ ] Prints clear next steps after completion
- [ ] Works on **Ubuntu/Debian/WSL2**
- [ ] Works on **macOS** (brew paths)

---

## Environment & Server Startup

- [ ] Server starts with env vars from `.env`
- [ ] Server starts with env vars exported in shell (no `.env`)
- [ ] Correct error message if DB connection fails (wrong password, DB doesn't exist)
- [ ] Correct error message if port already in use
- [ ] `uvicorn[standard]` installed — WebSocket mana works
- [ ] `--reload` flag works for dev mode
- [ ] Cookie secure flag behaves correctly (`OLYMPUSREPO_COOKIE_SECURE=1`)

---

## Authentication

- [ ] Login with zeus / initial password works
- [ ] Password change works
- [ ] Login fails correctly with wrong password (no stack trace exposed)
- [ ] Session expires correctly
- [ ] Logout clears session
- [ ] Password reset token flow works (console output)

---

## CLI — All 14 Commands

- [ ] `olympusrepo init` — creates repo, `.olympusrepo/` dir, config
- [ ] `olympusrepo add .` — stages all files
- [ ] `olympusrepo add <file>` — stages single file
- [ ] `olympusrepo status` — shows staged/unstaged correctly
- [ ] `olympusrepo commit -m "msg"` — commits, prints rev
- [ ] `olympusrepo log` — shows history
- [ ] `olympusrepo diff` — shows working tree diff
- [ ] `olympusrepo branch <name>` — creates branch
- [ ] `olympusrepo switch <name>` — switches branch, updates working tree
- [ ] `olympusrepo resolve <file>` — marks conflict resolved
- [ ] `olympusrepo mana` — posts/lists mana (if implemented)
- [ ] `olympusrepo remote add <name> <url>` — registers remote
- [ ] `olympusrepo remote list` — lists remotes
- [ ] `olympusrepo remote remove <name>` — removes remote
- [ ] `olympusrepo clone <url> <dir>` — clones repo, correct file paths (no prefix bug)
- [ ] `olympusrepo pull` — syncs from canonical, auto-creates repo record
- [ ] `olympusrepo offer -m "msg"` — sends staging realm to canonical
- [ ] `olympusrepo delete-repo` — removes repo cleanly

---

## CLI — Error Handling

- [ ] Commands run outside a repo give clear "Not in a repository" message
- [ ] `commit` with nothing staged gives clear message
- [ ] `switch` to nonexistent branch gives clear message
- [ ] `clone` with unreachable URL gives clear message, no stack trace
- [ ] `pull` with no remote configured gives clear message
- [ ] `offer` with no remote configured gives clear message
- [ ] Missing `.env` / unset env vars give clear message on startup

---

## Connector Flow (Two-Instance)

- [ ] Olympus (port 8000) and Athens (port 8001) both start cleanly
- [ ] `olympusrepo clone http://localhost:8000/repo/<name>` works
- [ ] Cloned files have **no path prefix bug** (files not showing as deleted in status)
- [ ] `olympusrepo pull` fetches new commits from canonical
- [ ] `olympusrepo offer` sends staging realm to canonical
- [ ] Offer appears in Olympus Staging tab
- [ ] Promote route works — `POST /api/repos/{name}/promote/{staging_id}`
- [ ] Canonical rev increments after promotion
- [ ] Audit log records promotion

---

## Web UI Smoke Test

- [ ] Repo browser loads — Files, Commits, Mana, Staging, Issues, Access, Settings tabs
- [ ] File upload (drag and drop single file) works
- [ ] Commit history shows rev tags and hashes
- [ ] Staging review page loads with side-by-side diff
- [ ] Promote modal fires correctly
- [ ] Mana posts in real time (WebSocket)
- [ ] Notification bell clears on read
- [ ] Message notification clears on read
- [ ] Issue create / comment / close works
- [ ] Zeus dashboard loads — stats, audit log, user management

---

## Known Bugs to Verify Fixed

- [ ] Promotion route `POST /api/repos/{name}/promote/{staging_id}` present in app.py
- [ ] Notification polling interval — 300000ms (5 min), not 60000ms
- [ ] Message notification badge clears after reading thread
- [ ] Clone path prefix stripped correctly
- [ ] `olympusrepo pull` auto-creates repo record
- [ ] `objects/` in DEFAULT_IGNORE_PATTERNS
- [ ] `claude-` test repo removed from DB
- [ ] `_bump_file_revs` verified working after first commit
- [ ] Folder drag-and-drop JS applied to repo_browser.html