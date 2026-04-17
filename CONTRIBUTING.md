# Contributing to OlympusRepo

Welcome. This is a sovereign version control system built on PostgreSQL. No corporate hooks, no subscriptions, no microservice sprawl. If that resonates with you, read on.

---

## The Philosophy

OlympusRepo is built on a few ideas that are non-negotiable:

**One database does everything.** Postgres handles users, auth, sessions, commits, blobs metadata, discussion, notifications, audit logs, and row-level security. We don't add Redis, Elasticsearch, or any other service unless Postgres genuinely cannot do the job. It can almost always do the job.

**The hierarchy is real.** Not everyone on a team has equal power over the canonical codebase. OlympusRepo encodes that reality instead of pretending everyone is equal and bolting on branch protection rules as an afterthought. Zeus owns the canonical tree. Contributors offer changes. Olympians review and promote. The words matter.

**Mana is permanent.** Design rationale lives attached to the code forever. There is no delete. If you made a decision, explain it. Future contributors deserve to know why.

**The master/slave model is intentional.** Canonical is truth. Slaves offer, never push. Nothing enters the canonical tree without review.

---

## The Stack

- **Python 3.10+** — no magic, readable code
- **PostgreSQL 14+** — via psycopg2, parameterized queries only, no ORM
- **FastAPI** — web layer, thin routes, no business logic in routes
- **Jinja2** — server-side templates, no frontend framework
- **Vanilla JS** — no React, no build step, no node_modules
- **diff3** — for three-way merges (install via diffutils)

---

## Getting Started

### Prerequisites

```bash
# Ubuntu/Debian/WSL2
sudo apt install postgresql postgresql-contrib python3 python3-venv diffutils

# macOS
brew install postgresql@16 python3 diffutils
```

### Clone and install

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install 'uvicorn[standard]'  # needed for WebSocket mana
```

### Database setup

```bash
sudo -u postgres psql -c "CREATE USER olympus WITH PASSWORD 'olympus' SUPERUSER;"
sudo -u postgres createdb -O olympus olympusrepo

for f in sql/0*.sql; do echo "Running $f..."; psql olympusrepo < "$f"; done
```

### Environment

```bash
export OLYMPUSREPO_DB_NAME=olympusrepo
export OLYMPUSREPO_DB_USER=olympus
export OLYMPUSREPO_DB_PASS=olympus
export OLYMPUSREPO_DB_HOST=127.0.0.1
export OLYMPUSREPO_DB_PORT=5432
```

Add these to your `~/.bashrc` so you don't have to set them every session.

### Start the server

```bash
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`. Login as `zeus` / `changeme`. **Change the password immediately.**

---

## Two-Instance Setup (Olympus + Athens)

This is how you test the connector — two independent instances talking to each other.

**Terminal 1 — Mount Olympus (canonical, port 8000):**
```bash
# Already running from above
```

**Terminal 2 — Athens (slave, port 8001):**
```bash
# Create slave database
sudo -u postgres createdb -O olympus olympusrepo_slave
for f in sql/0*.sql; do psql olympusrepo_slave < "$f"; done

# Start slave web server
OLYMPUSREPO_DB_NAME=olympusrepo_slave \
OLYMPUSREPO_DB_PASS=olympus \
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8001 --reload
```

**Terminal 3 — Athens CLI:**
```bash
source .venv/bin/activate
export OLYMPUSREPO_DB_NAME=olympusrepo_slave
export OLYMPUSREPO_DB_PASS=olympus

# Clone a repo from Olympus
olympusrepo clone http://localhost:8000/repo/<reponame> mylocal

cd mylocal
# make changes
echo "hello" > test.txt
olympusrepo add .
olympusrepo commit -m "my contribution"

# Offer to canonical for review
olympusrepo offer -m "here is why this should be accepted"
```

**On Olympus (localhost:8000):**
Go to the repo → Staging tab. You will see the offer arrive. Zeus reviews and promotes.

---

## Project Structure

```
olympusrepo/
├── olympusrepo/           # Python package
│   ├── cli.py             # All CLI commands
│   ├── core/
│   │   ├── db.py          # DB connection, queries, auth helpers
│   │   ├── diff.py        # Unified diff, three-way merge, side-by-side
│   │   ├── objects.py     # Content-addressable blob store
│   │   ├── repo.py        # High-level repo operations
│   │   └── worktree.py    # Working tree, index, ignore patterns
│   └── web/
│       └── app.py         # FastAPI routes (all of them)
├── sql/                   # Numbered migrations, run in order
├── templates/             # Jinja2 HTML templates
├── static/                # Background images, served at /static/
└── docs/                  # Documentation
```

### Key files to understand first

1. `sql/002_tables.sql` — the full schema. Read this first. Everything flows from here.
2. `olympusrepo/core/db.py` — how we talk to Postgres. All queries go through here.
3. `olympusrepo/core/repo.py` — commit(), commit_files(), create_repo(). The core logic.
4. `olympusrepo/web/app.py` — every web route. Large file, well-sectioned.
5. `olympusrepo/cli.py` — every CLI command.

---

## Areas That Need Work

These are confirmed gaps. Pick one, fix it, offer it.

### High Priority

**Promote route** (`app.py`)
The `POST /api/repos/{name}/promote/{staging_id}` route is missing. The code for it is written and documented in `docs/PROGRESS.md`. This is blocking the full connector workflow.

**Folder drag-and-drop upload** (`templates/repo_browser.html`)
The `webkitGetAsEntry` recursive folder reader needs to be wired into the upload section of `repo_browser.html`. The JS is written in `docs/PROGRESS.md`. The IDs need to match: `upload-drop-zone`, `folder-input`, `file-preview`, `upload-progress`, `upload-error`, `upload-commit-btn`.

**Message notification not clearing** (`app.py`)
When a user opens a message thread, the notification bell badge should clear. In `message_thread_page`, after the read receipt insert, add an UPDATE to `repo_notifications` setting `is_read = TRUE` where `link` matches the message URL.

**Notification polling too frequent** (`templates/base.html`)
The bell and message badge poll every 60 seconds. Change both `setInterval` calls from `60000` to `300000`.

**Clone path prefix** (`olympusrepo/cli.py`)
When you clone a repo that was originally committed with a parent folder included, all files get the folder name as a path prefix. The `cmd_clone` index builder needs to strip the common path prefix when building the local index.

### Medium Priority

**Setup script** (`setup.sh`)
One-shot installer. Check prerequisites, create DB user and database, run migrations, create venv, pip install, prompt for zeus username and password (no default `changeme`), write `.env` file, print next steps.

**`olympusrepo mana` CLI command**
Post and list mana from the command line. Schema and routes exist. CLI command needs adding to `cli.py`. See `docs/PROGRESS.md` patch set F for the implementation.

**`olympusrepo issue` CLI commands**
`new`, `list`, `close`, `assign`. Schema exists (`repo_issues`), routes exist. CLI commands missing.

**Type-ahead user search** (`templates/repo_access.html`, `templates/inbox.html`)
Replace `<select>` dropdowns for user selection with a debounced search input hitting `GET /api/users/search?q=`. The endpoint exists in `app.py`. The HTML for the input pattern is in `docs/PROGRESS.md`.

**`objects/` in DEFAULT_IGNORE_PATTERNS** (`olympusrepo/core/worktree.py`)
The local blob store directory gets committed accidentally. Add `"objects"` to `DEFAULT_IGNORE_PATTERNS` in `worktree.py`.

**ON DELETE CASCADE fixes** (`sql/010_fix_fk_cascades.sql`)
Several FK constraints block repo deletion. `repo_audit_log.repo_id` should be `ON DELETE SET NULL`. `repo_refs.commit_hash` and related FKs need review. File is documented in code, needs writing and running.

### Lower Priority

**Build system** (`sql/015_builds.sql`)
See `docs/BUILD_SYSTEM.md` for full design. Phase 1 is the issue tracker (already done). Phase 2 is build webhooks, rev tags (BROKEN/STABLE), file fault badges. Not urgent.

**WebSocket mana on slave instances**
The slave web server at port 8001 needs `uvicorn[standard]` installed and the WebSocket route confirmed working independently.

**User profile pages**
`/user/{username}` — shows recent commits, repos owned, role. Template `user_profile.html` is written, route exists in `app.py`, needs end-to-end test.

**Audit log export**
The audit log page has a hint about `\copy` but no actual export button. Add a `GET /api/admin/audit/export` endpoint that streams CSV.

---

## Code Standards

**No string concatenation in SQL.** Every query uses `%s` parameters. No exceptions. This is the entire SQL injection defense.

**Transactions are explicit.** Multi-step writes use `commit=False` on each `db.execute()` call and a single `conn.commit()` at the end, with `conn.rollback()` in the except block. Never rely on autocommit for anything that touches more than one table.

**Routes are thin.** Business logic lives in `core/repo.py` and `core/db.py`. Routes validate input, call core functions, return responses. If a route is getting long, something belongs in core.

**Templates get context dicts, not objects.** Pass plain dicts from routes to templates. Never pass SQLAlchemy models or psycopg2 row objects directly (we already use RealDictCursor which returns dicts, keep it that way).

**No frontend frameworks.** Vanilla JS only. If you need reactivity, use fetch + DOM manipulation. If the page is getting complex enough to need React, the server-side template is doing too little.

---

## The Offer Workflow

This is how contributions work in OlympusRepo itself:

```bash
# Clone the repo
olympusrepo clone http://canonical-server/repo/OlympusRepo mywork
cd mywork

# Make your changes
# ... edit files ...

# Commit locally
olympusrepo add .
olympusrepo commit -m "what you did and why (mana optional but encouraged)"

# Offer to canonical
olympusrepo offer -m "why this should be accepted and what you tested"
```

Zeus or an Olympian will review your offer on the canonical instance, leave mana comments if needed, and promote it when it's ready.

**Do not open GitHub pull requests for code changes.** Use the offer workflow. The repo on GitHub is a mirror for visibility. The canonical OlympusRepo instance is the source of truth.

---

## License

MIT. Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering.

You can fork it, host it, sell it, modify it. The only thing you can't do is pretend you wrote it.

---

*The Thunderer watches all.*
