# OlympusRepo

**Sovereign version control. No corporate hooks. No subscriptions.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL 14+](https://img.shields.io/badge/postgres-14+-336791.svg)](https://www.postgresql.org/)
[![Status: Beta v2.0](https://img.shields.io/badge/status-beta%20v2.0-blue.svg)]()

OlympusRepo is a centralized, review-driven VCS with a clear power hierarchy, built-in design discussion preservation, and full self-hosting independence. It's git's content-addressable storage married to SVN's sanity, with Postgres doing all the heavy lifting the rest of your stack pretends it needs microservices for.

**v2.0 adds a full git connector** — import any git repository with original SHAs preserved, round-trip push/pull to GitHub or any remote, and a smart-HTTP server so `git clone http://yourbox/repo.git` Just Works.

> *The Thunderer watches all.*

---

## Why?

Modern version control has been captured by one platform. Modern backend architecture has been captured by a dozen SaaS vendors. OlympusRepo is a push back against both:

- **One database.** Postgres handles users, auth, sessions, commits, blobs metadata, discussion, notifications, bug tracking, audit logs, and row-level security. No Redis. No Elasticsearch. No MongoDB. No Auth0.
- **One language.** Pure Python. Install it, run it, read it.
- **One workflow.** You commit to your own staging realm. A reviewer promotes it to the canonical tree. Every discussion about the code stays attached to the code forever.
- **One protocol bridge.** Talks to git natively — import, clone, push, pull — without any of git's accidental complexity leaking into your daily flow.
- **Self-hosted by default.** There is no hosted version. There is no company you have to trust. You run it. You own it.
- **Federated by design.** Two instances can talk to each other. Contributors offer changes from their local instance to a canonical instance. Zeus decides what gets in.

---

## What's New in v2.0 (the git connector)

| Feature | Status |
|---|---|
| **Import any git repo with full fidelity** — original commit SHAs, tree hashes, parents (incl. merge commits), author/committer identity + email + timezone offset preserved | ✓ |
| **Tree materialization** — reconstruct the full file tree at any commit, walking changeset deltas back to imported snapshot anchors | ✓ |
| **Push back to a git remote** — round-trip to GitHub/GitLab/etc. via `git fast-import` + `git push`, SHAs match originals when metadata survived | ✓ |
| **Pull from a git remote** — incremental fetch into the bare mirror cache, only new commits get imported | ✓ |
| **Smart-HTTP server** — `git clone http://yourbox:8000/<repo>.git` works from anywhere; `git push` requires PAT or basic auth | ✓ |
| **Personal Access Tokens** — `olyp_…` prefixed tokens for git push and API auth | ✓ |
| **Per-repo remote configuration** — encrypted credential storage (token / SSH key), audit log of every push/pull attempt | ✓ |
| **Niggles tightening** — `file_mode` per file (executable bit + symlinks survive), GPG signature passthrough, dangling-parent integrity view, log retention pruning | ✓ |

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

Contributors on remote machines have two ways to participate:

- **Native federation** — `olympusrepo offer` sends work to the canonical instance for review.
- **Standard git** — `git clone http://canonical:8000/<repo>.git`, work locally with any git tooling, then `git push` (auth required) or open a contribution flow.

All discussion is preserved forever. Every promotion is audited. Nothing is ephemeral.

---

## What's Built

### Core
- **Native CLI** — init, add, commit, status, log, diff, branch, switch, resolve, mana, clone, pull, offer, delete-repo
- **Web UI** — repo browser, file viewer with inline comments, commit history, side-by-side diff review, staging realms, mana discussion, threaded direct messaging, notifications
- **Bug tracker** — issues with file attachments, commit auto-linking (`fixes #N`), comments, priority, assignment
- **Federation** — clone from remote, pull updates, offer changes for review, full two-instance flow
- **Zeus Dashboard** — instance stats, audit log with filters, user management, server config
- **Mythological UI** — 14 backgrounds, choose-your-fate login, Hades 404

### v2.0 Git Connector
- **Import-from-URL** form (`/import`) — paste a git URL or local path, hit go, browse the imported repo a few seconds later
- **Per-repo remotes UI** (`/repo/<name>/remotes`) — add/delete remotes, push, pull, view audit log
- **Smart-HTTP endpoints** (`/<repo>.git/info/refs`, `/git-upload-pack`, `/git-receive-pack`) — drop-in compatible with any git client
- **PAT management** — generate, revoke, scope (`git:read`, `git:write`, `api:read`, `api:write`)
- **Bare-mirror gateway** per repo — derived state, rebuilt on demand from canonical commits

---

## Quick Start

### Prerequisites

```bash
# Ubuntu/Debian/WSL2
sudo apt install postgresql postgresql-contrib python3 python3-venv python3-pip git diffutils

# macOS
brew install postgresql@16 python3 git diffutils
```

### One-shot setup

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo
./setup.sh
```

The script walks you through database creation, schema migration (017 migrations as of v2.0), object/mirror/gateway directory layout, the `.env` file, and creating the first user. Takes about 2 minutes.

### Manual setup (if you don't trust scripts)

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo

sudo -u postgres psql -c "CREATE USER olympus WITH PASSWORD 'olympus' SUPERUSER;"
sudo -u postgres createdb -O olympus olympusrepo

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install 'uvicorn[standard]'

for f in sql/0*.sql; do PGPASSWORD=olympus psql -h 127.0.0.1 -U olympus olympusrepo < "$f"; done

mkdir -p objects mirrors gateways
cat > .env <<'EOF'
OLYMPUSREPO_DB_NAME=olympusrepo
OLYMPUSREPO_DB_USER=olympus
OLYMPUSREPO_DB_PASS=olympus
OLYMPUSREPO_DB_HOST=127.0.0.1
OLYMPUSREPO_DB_PORT=5432
OLYMPUSREPO_PORT=8000
OLYMPUSREPO_OBJECTS_DIR=./objects
OLYMPUSREPO_MIRRORS_DIR=./mirrors
OLYMPUSREPO_GATEWAYS_ROOT=./gateways
EOF
```

### Start

```bash
set -a; source .env; set +a
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. Login as your first user. Change the password.

---

## Two-Minute Tour (the v2 features)

```bash
# 1. Import a git repo via the web UI
#    Browser:  http://localhost:8000/import
#    Paste:    https://github.com/torvalds/linux.git    (or any small repo)
#              my-imported-repo
#    Click:    ★ Import Repository ★
# Comes back in seconds (small repos) to a few minutes (Linux kernel).
# All commits show with original SHAs.

# 2. Clone it back out via standard git — no extra tools, no auth needed for public
git clone http://localhost:8000/my-imported-repo.git
cd my-imported-repo
git log --oneline | head    # Same SHAs you imported

# 3. Configure a push-back to GitHub
#    Browser:  http://localhost:8000/repo/my-imported-repo/remotes
#    Add remote:
#       Name:   origin
#       URL:    https://github.com/you/your-fork.git
#       Auth:   Personal access token   (paste a GitHub PAT)
#    Click Push.

# 4. Pull updates from the configured remote
#    Same page, hit Pull. Only new commits get imported.
```

---

## Native CLI Reference

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
olympusrepo clone <url> [dest]       Clone from another OlympusRepo instance
olympusrepo pull [--remote origin]   Pull from canonical
olympusrepo offer [-m "why"]         Offer commits for review
olympusrepo remote add <name> <url>  Add a federation remote
olympusrepo mana [-m "message"]      Post or list design discussion
olympusrepo user-create <user> <pw>  Create a user (Zeus only)
olympusrepo delete-repo <name>       Delete a repository (Zeus only)
```

No `rebase`, no `cherry-pick`, no `reflog`, no `reset --mixed`. Commands do what their names say.

For interop with the wider ecosystem, the smart-HTTP server speaks the standard git protocol — use `git clone`, `git push`, `git pull` directly against `http://yourbox:8000/<repo>.git`.

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

The contributor can never push directly via the federation flow. They can only offer. Zeus decides what enters.

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
│   + smart-HTTP /git endpoints (NEW v2)          │
└────────────────┬────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────┐
│          Core Library (Python)                  │
│   objects, worktree, diff, repo operations      │
│   + import_git, export_git, pull_git (NEW v2)   │
│   + materialize, gateway, pats     (NEW v2)     │
└────────┬───────────────────┬────────────────────┘
         │                   │
┌────────▼─────────┐   ┌─────▼──────────────────────┐
│ Content-addr.    │   │   PostgreSQL               │
│ object store     │   │   users, sessions, commits │
│ (SHA-256 loose   │   │   blobs metadata, issues   │
│  objects on disk)│   │   mana, messages, audit    │
└──────────────────┘   │   git_remotes, gateways,   │
                       │   pats, push/pull logs     │
                       └────────────────────────────┘
                                 ▲
                                 │
                       ┌─────────┴──────────┐
                       │ External git host  │
                       │ (GitHub, GitLab,   │
                       │  Forgejo, etc.)    │
                       └────────────────────┘
                          push / pull only
```

Bare-mirror gateways live on disk per repo (`gateways/repo_<id>.git`). They're derived state — wiped and rebuilt by `gateway.ensure_gateway_synced()` from the canonical Postgres rows. The smart-HTTP server runs `git upload-pack` / `git receive-pack` against these gateways; receive-pack outputs are reingested back into Postgres.

---

## Status

**Beta v2.0.** Core workflow proven end to end. Two instances talking. Offers flowing. Zeus promoting. Git connector live: import, clone via HTTP, push back to GitHub all working.

**Recently fixed (v2.0 release shake-out):**
- DM "not sent" false error (asyncio.create_task in sync handler)
- DM body mid-flush truncation (Content-Length offset bug)
- Smart-HTTP empty pack on clone (gateway HEAD, fast-import close ordering, request body buffering)

**Known gaps (good first contributions):**
- Per-remote push/pull UI is functional but minimal — needs ahead/behind counts, branch dropdowns, force-push confirmation, action history
- Folder drag-and-drop upload JS still needs wiring
- Notification polling interval too aggressive (60s → 5min)

See `docs/CONTRIBUTING.md` for the full list with file locations and implementation notes.

---

## Contributing

Read `docs/CONTRIBUTING.md` first. It covers the philosophy, setup, two-instance test environment, exactly what's broken, where the code is, and how to contribute using OlympusRepo itself.

The short version: fix something from the known gaps list, commit it, offer it via `olympusrepo offer`. Don't open GitHub PRs for code — use the tool.

---

## Security

bcrypt via pgcrypto, 64-char hex session tokens from `gen_random_bytes`, `httponly` + `samesite=strict` cookies, row-level security for private messages. Git remote credentials encrypted at rest with `pgp_sym_encrypt` keyed off `repo_server_config['git_creds_key']` (random per install). Personal access tokens hashed with bcrypt before storage. Email security issues directly rather than filing public issues.

---

## License

MIT. See [LICENSE](LICENSE).

Copyright (c) 2 Paws Machine and Engineering.

---

## Acknowledgments

- **Postgres.** Does everything. You don't need the other things.
- **git**, for the content-addressable storage idea (and the fast-import format that lets us round-trip to it).
- **SVN**, for proving linear history isn't a crime.
- **Greek mythology**, for having better role names than "maintainer."
- **The "I replaced my entire stack with Postgres" video**, for the thesis statement.
