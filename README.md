# OlympusRepo

**Sovereign version control. No corporate hooks. No subscriptions.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL 14+](https://img.shields.io/badge/postgres-14+-336791.svg)](https://www.postgresql.org/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)]()

OlympusRepo is a centralized, review-driven VCS with a clear power hierarchy, built-in design discussion preservation, and full self-hosting independence. It's git's content-addressable storage married to SVN's sanity, with Postgres doing all the heavy lifting the rest of your stack pretends it needs microservices for.

> *The Thunderer watches all.*

---

## Why?

Modern version control has been captured by one platform. Modern backend architecture has been captured by a dozen SaaS vendors. OlympusRepo is a push back against both:

- **One database.** Postgres handles users, auth, sessions, metadata, audit logs, discussion, full-text search, and row-level security. No Redis. No Elasticsearch. No MongoDB. No Auth0.
- **One language.** Pure Python. Install it, run it, read it.
- **One workflow.** You commit to your own staging realm. A reviewer promotes it to the canonical tree. Every discussion about the code stays attached to the code forever.
- **Self-hosted by default.** There is no hosted version. There is no company you have to trust. You run it. You own it.

---

## The Hierarchy

OlympusRepo uses Greek mythology for roles because "maintainer" and "contributor" don't capture how power actually flows in a well-run project.

| Role | Who | Can Do |
|------|-----|--------|
| **Zeus** | Repo owner | Everything. Controls the canonical tree, permissions, visibility. |
| **Olympian** | Senior devs | Review and promote offerings to the canonical tree (scoped by Zeus). |
| **Titan** | Regular contributors | Commit to personal staging realm. |
| **Mortal** | Junior devs / guests | Limited staging. |
| **Prometheus** | Experimental coder | Isolated sandbox for risky changes. |
| **Hermes** | Hotfix contributor | Emergency fast-path patches. |

---

## How It Works

1. A **Titan** writes code and runs `olympusrepo commit -m "message"`.
2. Changes go to the Titan's **personal staging realm**, not the canonical tree.
3. An **Olympian** reviews the offering and discusses it via **mana** — comments attached to the specific file, commit, or staging realm.
4. The Olympian **promotes** the offering. The canonical tree gets a new commit with a global sequential revision number.
5. **Zeus** controls who can promote, to which branches, and the long-term tree shape.

All discussion is preserved forever. Every promotion is audited. Nothing is ephemeral.

---

## Quick Start

### Prerequisites

- PostgreSQL 14 or newer
- Python 3.10 or newer
- `diff3` (from `diffutils`) for three-way merges

```bash
# Install diff3 if you don't have it
apt-get install diffutils      # Debian/Ubuntu
brew install diffutils         # macOS
yum install diffutils          # RHEL/Fedora
```

### Install

```bash
git clone https://github.com/YOUR_USERNAME/olympusrepo.git
cd olympusrepo

# Create the database
createdb olympusrepo

# Run migrations in order
for f in sql/0*.sql; do psql olympusrepo < "$f"; done

# Install the Python package
pip install -e .
```

The seed script (`sql/007_seed.sql`) creates a default `zeus` user with password `changeme`. **Change this immediately after first login.**

### Configuration

OlympusRepo reads database connection info from environment variables:

```bash
export OLYMPUSREPO_DB_NAME=olympusrepo
export OLYMPUSREPO_DB_USER=olympus
export OLYMPUSREPO_DB_PASS=your_password
export OLYMPUSREPO_DB_HOST=127.0.0.1
export OLYMPUSREPO_DB_PORT=5432

# Production only: set secure cookie flag (requires HTTPS)
export OLYMPUSREPO_COOKIE_SECURE=1
```

### First Repository

```bash
# Create a user (if you didn't use the seed)
olympusrepo user-create alice strongpassword --role titan

# Initialize a new repo
olympusrepo init my-project --user alice
cd my-project

# Create some content
echo "# My Project" > README.md

# Stage and commit
olympusrepo add .
olympusrepo commit -m "Initial commit"
olympusrepo log
```

### Start the Web Server

```bash
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000.

---

## CLI Reference

```
olympusrepo init <name>              Create a new repository
olympusrepo add [files...]           Stage files for commit
olympusrepo commit -m "message"      Commit staged changes
olympusrepo status                   Show working tree status
olympusrepo log [--limit N] [--path] Show commit history
olympusrepo diff [file]              Show working tree diffs
olympusrepo branch [name]            List or create branches
olympusrepo switch <branch>          Switch branches
olympusrepo resolve <file>           Mark a conflicted file resolved
olympusrepo user-create <user> <pw>  Create a user (admin)
```

No `rebase`, no `cherry-pick`, no `reflog`, no `reset --mixed`. Commands do what their names say.

---

## Ignoring Files

OlympusRepo ignores common artifacts by default: `.git`, `__pycache__`, `*.pyc`, `node_modules`, `.venv`, `.DS_Store`, and similar. Add a `.olympusignore` file at your repo root for project-specific patterns. Syntax is glob-based (fnmatch), one pattern per line, `#` for comments.

```
# .olympusignore example
build/
dist/
*.log
secrets.env
```

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Browser / CLI Client               │
└────────────────┬────────────────────────────────┘
                 │ HTTP / HTTPS
┌────────────────▼────────────────────────────────┐
│         FastAPI Web Server (Python)             │
│   auth, routes, templates, RLS context          │
└────────────────┬────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────┐
│          Core Library (Python)                  │
│   objects, worktree, diff, repo operations      │
└────────┬───────────────────┬────────────────────┘
         │                   │
┌────────▼─────────┐   ┌─────▼──────────────────┐
│ Content-addr.    │   │   PostgreSQL           │
│ object store     │   │   users, sessions,     │
│ (SHA-256 loose   │   │   commits, changesets, │
│  objects on disk)│   │   refs, mana, audit    │
└──────────────────┘   └────────────────────────┘
```

### Layers

- **Core** (`olympusrepo/core/`) — pure Python, no framework dependencies. Object store, working tree scanner, diff/merge, high-level repo operations.
- **CLI** (`olympusrepo/cli.py`) — argparse-based command-line interface.
- **Web** (`olympusrepo/web/`) — FastAPI app. Session auth, API endpoints, HTML templates.
- **Schema** (`sql/`) — 17 tables, numbered migrations, RLS policies for private messages, helper functions for auth.

### Storage

Blobs are stored as loose objects on disk at `.olympusrepo/objects/<2-char-prefix>/<rest-of-hash>`. Content-addressable and idempotent — the same file always hashes to the same object.

Metadata (commits, branches, users, discussion, audit log) lives in Postgres.

---

## Project Structure

```
olympusrepo/
├── olympusrepo/              # Python package
│   ├── core/                 # Object store, worktree, diff, repo ops
│   │   ├── db.py
│   │   ├── objects.py
│   │   ├── diff.py
│   │   ├── worktree.py
│   │   └── repo.py
│   ├── web/                  # FastAPI server
│   │   └── app.py
│   ├── cli.py                # CLI entry point
│   └── __main__.py           # python -m olympusrepo
├── sql/                      # Database migrations
│   ├── 001_extensions.sql
│   ├── 002_tables.sql
│   ├── 003_indexes.sql
│   ├── 004_rls.sql
│   ├── 005_functions.sql
│   ├── 006_defaults.sql
│   └── 007_seed.sql
├── templates/                # Jinja2 HTML
│   ├── base.html
│   ├── index.html
│   ├── login.html
│   ├── repo_browser.html
│   └── zeus_dashboard.html
├── docs/                     # Design documents
│   ├── ARCHITECTURE.md
│   ├── SCHEMA.md
│   └── API.md
├── setup.py
├── requirements.txt
├── LICENSE
└── README.md
```

---

## Status

**Alpha.** The core commit/log/branch/diff loop works. Three-way merge works if `diff3` is installed. The web UI shows repos, commits, and mana.

**Not yet implemented:**

- File tree loading in the repo browser (placeholder list)
- WebSocket mana streaming (the REST endpoint works)
- Clone / pack download endpoints
- Full staging realm workflow in the web UI (DB schema is ready)
- AILang binary core (the `repo_exec_tokens` table is reserved for it)

PRs welcome on any of these.

---

## Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feature/your-thing` *(yes, we still use git for the OlympusRepo repo itself — we're not that religious)*
3. Commit with clear messages
4. Open a pull request

Bug reports with steps to reproduce are especially appreciated. File under "Issues."

---

## Security

Auth uses bcrypt via pgcrypto, session tokens are 64-char hex from `gen_random_bytes`, cookies are `httponly` and `samesite=strict` (and `secure` when `OLYMPUSREPO_COOKIE_SECURE=1`). Row-level security enforces private message visibility at the database layer.

If you find a security issue, please email directly rather than filing a public issue.

---

## License

MIT. See [LICENSE](LICENSE).

Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering.

---

## Acknowledgments

- **Postgres.** Does everything. You don't need the other things.
- **git**, for the content-addressable storage idea.
- **SVN**, for proving linear history isn't a crime.
- **Greek mythology**, for having better role names than "maintainer."