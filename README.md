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

- **One database.** Postgres handles users, auth, sessions, commits, blobs metadata, discussion, notifications, bug tracking, audit logs, and row-level security. No Redis. No Elasticsearch. No MongoDB. No Auth0.
- **One language.** Pure Python. Install it, run it, read it.
- **One workflow.** You commit to your own staging realm. A reviewer promotes it to the canonical tree. Every discussion about the code stays attached to the code forever.
- **Self-hosted by default.** There is no hosted version. There is no company you have to trust. You run it. You own it.
- **Federated by design.** Two instances can talk to each other. Contributors offer changes from their local instance to a canonical instance. Zeus decides what gets in.

---

## The Hierarchy

OlympusRepo uses Greek mythology for roles because "maintainer" and "contributor" don't capture how power actually flows in a well-run project.

| Role | Who | Can Do |
|------|-----|--------|
| **Zeus** | Repo owner | Everything. Controls the canonical tree, permissions, visibility. |
| **Olympian** | Senior devs | Review and promote offerings to the canonical tree. |
| **Titan** | Regular contributors | Commit to personal staging realm. |
| **Mortal** | Junior devs / guests | Limited staging. |
| **Prometheus** | Experimental coder | Isolated sandbox for risky changes. |
| **Hermes** | Hotfix contributor | Emergency fast-path patches. |

Roles are extensible — it's a CHECK constraint on a text column. Add your own via migration.

---

## How It Works

1. A **Titan** writes code and runs `olympusrepo commit -m "message"`.
2. Changes go to the Titan's **personal staging realm**, not the canonical tree.
3. An **Olympian** reviews the offering via the side-by-side diff viewer and discusses it via **mana** — comments attached to the specific file, commit, or staging realm.
4. The Olympian **promotes** the offering. The canonical tree gets a new commit with a global sequential revision number.
5. **Zeus** controls who can promote, to which branches, and the long-term tree shape.

Contributors on remote machines use `olympusrepo offer` to send their work to canonical. Nothing enters the canonical tree without review. You can offer but never push.

All discussion is preserved forever. Every promotion is audited. Nothing is ephemeral.

---

## What's Built

- **Full CLI** — init, add, commit, status, log, diff, branch, switch, resolve, mana, clone, pull, offer, delete-repo
- **Web UI** — repo browser, file viewer with inline comments, commit history, side-by-side diff review, staging realms, mana discussion, direct messaging, notifications
- **Bug tracker** — issues with file attachments, commit auto-linking (`fixes #N`), comments, priority, assignment
- **Connector** — clone from remote, pull updates, offer changes for review, full two-instance federation
- **Zeus Dashboard** — instance stats, audit log with filters, user management, server config
- **Mythological UI** — 14 Grok-generated backgrounds, choose-your-fate login screen, Hades 404 page

---

## Quick Start

### Prerequisites

```bash
# Ubuntu/Debian/WSL2
sudo apt install postgresql postgresql-contrib python3 python3-venv diffutils

# macOS
brew install postgresql@16 python3 diffutils
```

### Install

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo

sudo -u postgres psql -c "CREATE USER olympus WITH PASSWORD 'olympus' SUPERUSER;"
sudo -u postgres createdb -O olympus olympusrepo

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install 'uvicorn[standard]'

for f in sql/0*.sql; do psql olympusrepo < "$f"; done
```

### Start

```bash
export OLYMPUSREPO_DB_PASS=olympus
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`. Login as `zeus` / `changeme`. Change the password.

See `docs/SETUP.md` for the full setup guide including WSL2 notes, macOS, and production checklist.

---

## CLI Reference

```
olympusrepo init <name>              Create a new repository
olympusrepo add [files...]           Stage files for commit
olympusrepo commit -m "message"      Commit staged changes
olympusrepo status                   Show working tree status
olympusrepo log                      Show commit history
olympusrepo diff [file]              Show differences
olympusrepo branch [name]            List or create branches
olympusrepo switch <branch>          Switch branches
olympusrepo resolve <file>           Mark conflict resolved
olympusrepo clone <url> [dest]       Clone from remote instance
olympusrepo pull [--remote origin]   Pull from canonical
olympusrepo offer [-m "why"]         Offer commits for review
olympusrepo remote add <name> <url>  Add a remote instance
olympusrepo mana [-m "message"]      Post or list design discussion
olympusrepo user-create <user> <pw>  Create a user (Zeus only)
olympusrepo delete-repo <name>       Delete a repository (Zeus only)
```

No `rebase`, no `cherry-pick`, no `reflog`, no `reset --mixed`. Commands do what their names say.

---

## Two-Instance Federation

```bash
# On contributor machine (Athens):
olympusrepo clone http://canonical:8000/repo/myproject
cd myproject
# ... make changes ...
olympusrepo commit -m "my fix - closes #12"
olympusrepo offer -m "tested on Ubuntu 24.04, all passing"

# On canonical (Olympus):
# Zeus sees the offer in the Staging tab
# Reviews side-by-side diff in the browser
# Clicks Promote ⚡
# Changes enter the canonical tree at the next global rev
```

The contributor can never push directly. They can only offer. Zeus decides what enters.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│         Athens (slave instance)                 │
│   olympusrepo clone / pull / offer              │
└────────────────┬────────────────────────────────┘
                 │ HTTP offer
┌────────────────▼────────────────────────────────┐
│         Olympus (canonical instance)            │
│   FastAPI + Jinja2 web server                   │
└────────────────┬────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────┐
│          Core Library (Python)                  │
│   objects, worktree, diff, repo operations      │
└────────┬───────────────────┬────────────────────┘
         │                   │
┌────────▼─────────┐   ┌─────▼──────────────────────┐
│ Content-addr.    │   │   PostgreSQL               │
│ object store     │   │   users, sessions, commits  │
│ (SHA-256 loose   │   │   blobs metadata, issues    │
│  objects on disk)│   │   mana, messages, audit     │
└──────────────────┘   └────────────────────────────┘
```

---

## Status

**Beta 1.0** Core workflow proven end to end. Two instances talking. Offers flowing. Zeus promoting.

**Known gaps (good first contributions):**
- Promote route needs applying (code written, see `docs/CONTRIBUTING.md`)
- Folder drag-and-drop upload JS needs wiring
- Setup script (`setup.sh`) not yet written
- Notification polling interval too aggressive (60s → 5min)
- Clone path prefix issue when committing whole folders

See `docs/CONTRIBUTING.md` for the full list with file locations and implementation notes.

---

## Contributing

Read `docs/CONTRIBUTING.md` first. It covers the philosophy, setup, two-instance test environment, exactly what's broken, where the code is, and how to contribute using OlympusRepo itself.

The short version: fix something from the known gaps list, commit it, offer it via `olympusrepo offer`. Don't open GitHub PRs for code — use the tool.

---

## Security

bcrypt via pgcrypto, 64-char hex session tokens from `gen_random_bytes`, `httponly` + `samesite=strict` cookies, row-level security for private messages. Email security issues directly rather than filing public issues.

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
- **The "I replaced my entire stack with Postgres" video**, for the thesis statement.
