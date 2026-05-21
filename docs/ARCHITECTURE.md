# OlympusRepo — System Architecture

**Comprehensive design reference for developers and AI agents.**
*Last updated: May 2026 — covers v2.1.x*

---

## Table of Contents

1. [Design Philosophy](#design-philosophy)
2. [System Overview](#system-overview)
3. [Directory Layout](#directory-layout)
4. [Storage Model](#storage-model)
5. [Database Schema](#database-schema)
6. [Core Library](#core-library)
7. [Commit Lifecycle](#commit-lifecycle)
8. [Tree Materialization](#tree-materialization)
9. [The Git Bridge](#the-git-bridge)
10. [Smart-HTTP Protocol](#smart-http-protocol)
11. [Federation (Olympus ↔ Athens)](#federation)
12. [OlympusRelay (Decentralized Discovery)](#olympusrelay)
13. [Web Application](#web-application)
14. [CLI](#cli)
15. [Authentication & Security](#authentication--security)
16. [Role Hierarchy](#role-hierarchy)
17. [API Surface](#api-surface)
18. [Configuration](#configuration)
19. [SQL Migrations](#sql-migrations)
20. [Extension Points](#extension-points)
21. [Known Constraints](#known-constraints)

---

## Design Philosophy

Five non-negotiable principles:

1. **One database.** PostgreSQL handles users, auth, sessions, commits, blob metadata, discussion, notifications, bug tracking, audit logs, and row-level security. No Redis. No Elasticsearch. No MongoDB.
2. **One language.** Pure Python. No ORM. All SQL is parameterized `%s` queries via psycopg2.
3. **Filesystem for content, database for metadata.** File bytes live as content-addressed blobs on disk. PostgreSQL stores hashes, paths, sizes, diffs, and relationships — never file content.
4. **Hierarchy is real.** Not everyone has equal power. Zeus owns the canonical tree. Contributors offer. Olympians review and promote. The words are deliberate.
5. **Mana is permanent.** Design rationale lives attached to the code forever. No delete. Future developers deserve to know why.

---

## System Overview

```
┌─────────────────────────────────────────────────────┐
│           Athens (contributor instance)              │
│   CLI: olympusrepo clone / pull / offer             │
│   Web: http://athens:8001                           │
└────────────────┬────────────────────────────────────┘
                 │ HTTP offer / sync
┌────────────────▼────────────────────────────────────┐
│           Olympus (canonical instance)              │
│   FastAPI + Jinja2 web server                       │
│   + smart-HTTP git endpoints (/<repo>.git)          │
│   + WebSocket mana (live discussion)                │
└──────┬──────────────────┬───────────────────────────┘
       │                  │
┌──────▼──────────┐  ┌────▼─────────────────────────┐
│ Content-addr.   │  │  PostgreSQL                   │
│ object store    │  │  ├─ repo_users, repo_sessions │
│ objects/        │  │  ├─ repo_repositories         │
│   ab/cdef01...  │  │  ├─ repo_commits              │
│   (SHA-256,     │  │  ├─ repo_changesets           │
│    gzip opt.)   │  │  ├─ repo_objects (hash→meta)  │
│                 │  │  ├─ repo_refs (branches)       │
│ gateways/       │  │  ├─ repo_staging              │
│   repo_N.git    │  │  ├─ repo_messages (mana)      │
│   (bare git,    │  │  ├─ repo_issues               │
│    derived)     │  │  ├─ repo_audit_log            │
│                 │  │  ├─ repo_git_remotes          │
│ mirrors/        │  │  ├─ repo_pats                 │
│   remote_N.git  │  │  └─ ... (30+ tables total)    │
│   (fetch cache) │  │                               │
└─────────────────┘  └───────────────────────────────┘
                              ▲
                              │ push / pull
                     ┌────────┴──────────┐
                     │ External git host │
                     │ (GitHub, GitLab,  │
                     │  Forgejo, etc.)   │
                     └───────────────────┘
```

---

## Directory Layout

```
OlympusRepo/
├── olympusrepo/                # Python package (pip install -e .)
│   ├── __init__.py
│   ├── __main__.py             # python -m olympusrepo entry point
│   ├── cli.py                  # All CLI commands (~900 lines)
│   ├── core/
│   │   ├── db.py               # Connection pool, query helpers, auth wrappers
│   │   ├── objects.py          # Content-addressable blob store (SHA-256)
│   │   ├── repo.py             # commit(), commit_files(), create_repo(), branches
│   │   ├── worktree.py         # Working tree, index, ignore patterns, change detection
│   │   ├── diff.py             # Unified diff, side-by-side diff, three-way merge (diff3)
│   │   ├── materialize.py      # Tree reconstruction from changeset chain
│   │   ├── import_git.py       # Git repository import (subprocess + cat-file --batch)
│   │   ├── export_git.py       # Push to git remote (fast-import + git push)
│   │   ├── pull_git.py         # Incremental pull from git remote via mirror cache
│   │   ├── gateway.py          # Per-repo bare git mirror for smart-HTTP
│   │   ├── git_remotes.py      # Remote CRUD + encrypted credential storage
│   │   ├── pats.py             # Personal Access Tokens (olyp_... prefix)
│   │   ├── identity.py         # Ed25519 instance identity for relay
│   │   └── fsck.py             # Integrity checker + orphan pruner
│   └── web/
│       ├── app.py              # FastAPI routes (~5000 lines, single router)
│       └── git_protocol.py     # Smart-HTTP endpoints (include_router)
├── sql/                        # 19 numbered migrations, run in order
│   ├── 001_extensions.sql      # pgcrypto
│   ├── 002_tables.sql          # Core schema (17 tables)
│   ├── 003_indexes.sql         # Query performance
│   ├── 004_rls.sql             # Row-level security for private mana
│   ├── 005_functions.sql       # repo_create_user, repo_verify_password, etc.
│   ├── 006_defaults.sql        # Default server config values
│   ├── 007_seed.sql            # Default zeus/changeme account
│   ├── 008_notifications.sql   # Notification system
│   ├── 009_password_reset.sql  # Password reset tokens
│   ├── 010_fix_fk_cascades.sql # FK cascade fixes for repo deletion
│   ├── 011_file_revisions.sql  # Per-file revision tracking
│   ├── 012_mana_threads.sql    # Threaded mana (parent_id, thread_id)
│   ├── 013_issues.sql          # Bug tracker tables
│   ├── 014_connector.sql       # Federation: remotes, sync state, offers
│   ├── 015_git_import.sql      # Full-fidelity git import schema
│   ├── 016_git_push.sql        # Git push/pull + encrypted credentials
│   ├── 017_phase4_and_tightening.sql  # file_mode, gpg_signature, gateway columns
│   ├── 018_anon_offerings.sql  # Anonymous drive-by contributions
│   └── 019_repair_v2_upload.sql # v2 web-upload byte_offset fix
├── templates/                  # 40+ Jinja2 HTML templates
├── static/                     # Background images, served at /static/
├── objects/                    # Blob store (created by setup.sh)
├── gateways/                   # Bare git repos for smart-HTTP (derived state)
├── mirrors/                    # Bare git repos for pull caching
├── relay/                      # Standalone olympusrelay service
├── scripts/                    # Deployment helpers
├── systemd/                    # systemd unit files
├── docs/                       # Documentation
├── setup.py                    # Package definition
├── setup.sh                    # One-shot installer
└── setup_wizard.py             # Interactive setup wizard
```

---

## Storage Model

### Blob Store (`core/objects.py`)

Files are stored as **content-addressed loose objects** on disk, keyed by their SHA-256 hash.

```
objects/
  a1/b2c3d4e5f6...     ← first 2 hex chars as fan-out directory
  ff/0a967b8c2d...
```

**Key functions:**

| Function | Purpose |
|----------|---------|
| `store_blob(content, objects_dir, compress)` | Write bytes → SHA-256 hash. Optional gzip. Idempotent — same content = same hash = no-op. |
| `store_file(filepath, objects_dir, compress)` | Read file from disk, delegate to `store_blob`. |
| `retrieve_blob(obj_hash, objects_dir)` | Read bytes by hash. Transparent gzip decompression. |
| `hash_content(content)` | SHA-256 hash without storing. |
| `hash_file(filepath)` | SHA-256 hash a file without storing. |
| `blob_exists(obj_hash, objects_dir)` | Check existence without reading. |

**Invariants:**
- Same content always produces the same hash (content-addressed).
- Blobs are shared across all repos on the same instance.
- The object store is the only place file bytes live. PostgreSQL never stores file content.
- Blobs may be gzip-compressed on disk; `retrieve_blob` handles decompression transparently.

### What PostgreSQL Stores (Metadata Only)

| Table | What it tracks |
|-------|---------------|
| `repo_objects` | `object_hash → repo_id, size_bytes, object_type` — registry, not content |
| `repo_commits` | Commit hash, tree hash, author, committer, message, parent DAG, timestamps |
| `repo_changesets` | Per-commit file deltas: path, change_type, `blob_before`/`blob_after` (hashes), lines +/- |
| `repo_refs` | Branch pointers: `ref_name → commit_hash` per repo |
| `repo_file_revisions` | Per-file revision history for timeline views |

### Gateway Repos (`gateways/`)

Bare git repos at `gateways/repo_<id>.git`. **Derived state** — rebuilt on demand from canonical Postgres data via `git fast-import`. Used exclusively by the smart-HTTP server for `git clone/fetch/push`. Can be deleted and rebuilt at any time.

### Mirror Repos (`mirrors/`)

Bare git repos cached per remote. Used by `pull_git.py` for incremental fetches — only new commits since last pull are imported.

---

## Database Schema

### Core Tables (002_tables.sql)

```
repo_users              Users with bcrypt passwords, roles, profile fields
repo_sessions           Session tokens (64-char hex, httponly cookies)
repo_server_config      Instance-wide key/value settings
repo_repositories       Repos: name, owner, visibility, description, default_branch
repo_access             Per-user repo access grants (for private repos)
repo_permissions        Per-user repo permissions (promote, branch_create, etc.)
repo_commits            Commit metadata + parent DAG + imported original SHAs
repo_changesets         Per-file deltas per commit (add/modify/delete/rename)
repo_objects            Blob hash registry (hash → repo + size + type)
repo_refs               Branch pointers (ref_name → commit_hash)
repo_staging            Staging realms (per-contributor working area)
repo_staging_changes    Changes within a staging realm
repo_messages           Mana: threaded discussion attached to repos/files/commits
repo_issues             Bug tracker with priority, assignment, status
repo_issue_comments     Issue discussion thread
repo_issue_attachments  File attachments on issues
repo_notifications      User notification queue
repo_audit_log          Every significant action recorded
repo_file_revisions     Per-file version timeline
```

### Git Connector Tables (015–018)

```
repo_git_remotes        Remote URL + encrypted credentials per repo
repo_git_push_log       Audit trail of every push attempt
repo_git_pull_log       Audit trail of every pull attempt
repo_pats               Personal Access Tokens (bcrypt-hashed, scoped)
repo_anon_rate_log      Rate limiting for anonymous offerings
```

### Schema Conventions

- All PKs are `SERIAL` or `BIGSERIAL`.
- All timestamps are `TIMESTAMPTZ DEFAULT now()`.
- `ON DELETE CASCADE` on child tables; `ON DELETE SET NULL` on audit_log.repo_id.
- Roles use `CHECK (role IN ('zeus','olympian','titan','mortal','prometheus','hermes'))`.
- No ORM. All queries are raw SQL with `%s` parameterization. No exceptions.

---

## Core Library

### `core/db.py` — Database Access

Thin wrapper around psycopg2. All queries go through these functions:

```python
connect()                                    # Returns connection, autocommit=False
execute(conn, sql, params, commit=True)      # Statement execution
query(conn, sql, params)                     # Returns list[dict] via RealDictCursor
query_one(conn, sql, params)                 # Returns dict or None
query_scalar(conn, sql, params)              # Returns single value or None
set_session_user(conn, user_id)              # Sets app.current_user_id for RLS
audit_log(conn, action, ...)                 # Writes to repo_audit_log
```

Auth helpers wrap SQL functions from 005_functions.sql:
```python
create_user(conn, username, password, role, ...)
verify_password(conn, username, password)    # Returns user_id or None
create_session(conn, user_id, ip, agent)     # Returns 64-char hex token
validate_session(conn, session_id)           # Returns user_id or None
```

**Transaction model:** `execute()` auto-commits by default. For multi-statement transactions, pass `commit=False` to each call, then `conn.commit()` at the end with `conn.rollback()` in the except block.

### `core/repo.py` — Repository Operations

High-level operations on repos and commits:

```python
create_repo(conn, name, owner_id, ...)       # Create repo + initial ref
get_repo(conn, name_or_id)                   # Lookup by name or ID
branches(conn, repo_id)                      # List branches
commit(conn, repo_id, user_id, message, changes, branch, objects_dir)
    # Atomic commit: store blobs, write changesets, update ref
commit_files(conn, repo_id, user_id, message, files_dict, branch, objects_dir)
    # Web upload path: dict of {path: bytes}
import_commit_row(conn, repo_id, commit_data, objects_dir)
    # Git import path: insert pre-hashed commit + changesets
set_ref(conn, repo_id, ref_name, commit_hash)
    # Update branch pointer
```

### `core/worktree.py` — Local Working Tree (CLI)

Manages the `.olympusrepo/` directory structure for CLI users:

```
.olympusrepo/
  config.json             Repo URL, user, branch, repo_id
  HEAD                    Current branch name
  index.json              Staged files: {path: {hash, mtime, size}}
  index_committed.json    Last committed state (diff/status baseline)
  .olympusignore          Ignore patterns (glob, one per line)
  pending/                Offline commits waiting to sync
  cache/                  Cached blobs for offline work
```

Key functions: `scan_working_tree()`, `detect_changes()`, `detect_staged_changes()`, `save_config()` (atomic via tmp+rename).

### `core/diff.py` — Diffing and Merging

```python
unified_diff(old_lines, new_lines)           # Standard unified diff
diff_content(old, new)                       # Diff strings → (text, +lines, -lines)
diff_side_by_side(old, new)                  # List of {left, right} pairs for web UI
merge_three_way(base, ours, theirs)          # Via system diff3 → (merged, has_conflicts)
has_conflict_markers(content)                # Check for <<<<<<< markers
```

Three-way merge requires `diff3` from diffutils. Falls back to fast paths when only one side changed.

---

## Commit Lifecycle

### Path 1: CLI Commit

```
User edits files
  → olympusrepo add .           (scan tree, hash files, write index.json)
  → olympusrepo commit -m "..."
      → worktree.detect_staged_changes()  (diff index vs committed_index)
      → for each changed file:
          objects.store_blob(content)       → disk (objects/ab/cdef...)
      → repo.commit(conn, changes)
          → INSERT repo_commits            (hash, tree_hash, author, message, parents)
          → INSERT repo_changesets         (path, change_type, blob_before, blob_after)
          → INSERT repo_objects            (hash → repo_id, size)
          → UPDATE repo_refs              (branch → new commit_hash)
      → worktree.save_committed_index()    (snapshot for next diff baseline)
```

### Path 2: Web Upload

```
User uploads file via browser
  → POST /api/repos/{name}/upload
      → objects.store_blob(file_bytes)      → disk
      → repo.commit_files(conn, {path: bytes})
          → Same INSERT chain as CLI
```

### Path 3: Git Import

```
POST /import  (paste a git URL)
  → import_git.import_repository(url, repo_name)
      → git clone --bare → temp dir
      → git rev-list --topo-order --reverse (all commits)
      → For each commit:
          git cat-file --batch (read every blob)
          objects.store_blob(content)        → disk
          repo.import_commit_row(conn, commit_data)
              → INSERT repo_commits (is_imported=TRUE, original SHA preserved)
              → INSERT repo_changesets (full snapshot for imported commits)
              → INSERT repo_objects
      → set_ref for each branch
```

### Path 4: Staging Realm → Promotion

```
Contributor commits to staging realm
  → INSERT repo_staging + repo_staging_changes
  → Olympian reviews at /repo/{name}/staging/{id}/review
  → POST /api/repos/{name}/promote/{staging_id}
      → Read staging changes
      → repo.commit() with those changes on canonical branch
      → Staging status → 'promoted'
```

---

## Tree Materialization

`core/materialize.py` reconstructs the full file tree at any commit by walking the changeset chain.

### Algorithm

```
materialize_tree(conn, repo_id, commit_hash):
    1. Walk first-parent chain backward from target commit
    2. Stop at an "anchor" — either:
       a. An imported commit (has full snapshot from git import)
       b. A root commit (no parents)
    3. Collect chain: [target, parent, grandparent, ..., anchor]
    4. Apply changesets forward from anchor to target
    5. Return {path: (blob_hash, file_mode)}
```

**Change types applied:**
- `add` / `modify` → set `tree[path] = (blob_after, mode)`
- `delete` → remove `tree[path]`
- `rename` → move `tree[old_path]` to `tree[path]`, optionally update content

**Used by:**
- File browser: `/repo/{name}/blob/{branch}/{path}`
- Edit-in-browser: `/repo/{name}/edit/{branch}/{path}`
- Export to git: `export_git._files_at_commit()`
- Gateway rebuild: `gateway.ensure_gateway_synced()`

---

## The Git Bridge

### Import (`core/import_git.py`)

Imports any git repository with full fidelity:

```
git clone --bare <url> → temp
git rev-list --topo-order --reverse → commit list
for each commit:
    git cat-file --batch → read all blobs
    git diff-tree -r → compute changeset
    store blobs to objects/
    INSERT commit + changesets + objects into PostgreSQL
```

**Preserved:** original commit SHAs, tree hashes, parent DAG (including merges), author/committer identity + email + timezone offset, GPG signatures, file modes.

**Guardrails:** `MAX_COMMITS` and `MAX_TOTAL_BYTES` env vars (default 0 = unlimited). Set via `OLYMPUSREPO_IMPORT_MAX_COMMITS` / `OLYMPUSREPO_IMPORT_MAX_BYTES` for shared instances.

### Export / Push (`core/export_git.py`)

Round-trips commits back to a git remote:

```
materialize tree at each commit → git fast-import stream → bare repo
git push <authenticated_url> <branch>
```

Credential handling via `git_remotes.build_authenticated_url()` — embeds token as `x-access-token:TOKEN@host` for HTTPS.

### Pull (`core/pull_git.py`)

Incremental pull using persistent bare mirror:

```
mirrors/remote_<id>.git  (persistent fetch cache)
git fetch → only new objects
git rev-list <old_tip>...<new_tip> → new commits only
import_commit_row() for each new commit
```

### Gateway (`core/gateway.py`)

Per-repo bare git mirror rebuilt on demand from canonical data:

```
gateways/repo_<id>.git
  - Created/rebuilt via git fast-import from materialize_tree()
  - Used by smart-HTTP for git clone/fetch/push
  - Derived state — delete and rebuild at any time
  - ensure_gateway_synced(conn, repo_id, force_rebuild=False)
```

---

## Smart-HTTP Protocol

`web/git_protocol.py` implements the git smart-HTTP transport:

```
GET  /<repo>.git/info/refs?service=git-upload-pack     → ref advertisement (clone/fetch)
GET  /<repo>.git/info/refs?service=git-receive-pack    → ref advertisement (push)
POST /<repo>.git/git-upload-pack                       → pack negotiation (clone/fetch)
POST /<repo>.git/git-receive-pack                      → receive push
```

**Flow:**
1. Client requests `info/refs` → server runs `git upload-pack --advertise-refs` against gateway
2. Client sends wants/haves → server runs `git upload-pack` with the request body
3. Server returns pack data

**Authentication:** Clone/fetch is public (read). Push requires PAT or session cookie. Verified via `pats.verify_pat()` or `db.validate_session()`.

**Limits:** `_MAX_RECEIVE_BYTES = 10 GiB` on push request body (defense against abuse, not a content limit).

**After push:** `reingest_from_gateway()` reads new commits from the gateway bare repo and imports them back into Postgres + blob store, keeping the canonical data model as source of truth.

---

## Federation

### Model

- **Canonical (Olympus)** — owns truth. Assigns sequential revision numbers at promotion.
- **Contributor (Athens)** — clones, works locally, offers changes back. Never pushes directly.

### Sync Protocol

```
GET  /api/sync/{name}/info                → repo metadata + latest commit hash
GET  /api/sync/{name}/commits             → commit list (paginated)
GET  /api/sync/{name}/blob/{hash}         → raw blob content
POST /api/sync/{name}/offer               → submit staging changes for review
```

### Clone Flow (Athens → Olympus)

```
olympusrepo clone http://olympus:8000/repo/myproject
  1. GET /api/sync/myproject/info          → repo_id, branches, tip
  2. GET /api/sync/myproject/commits       → all commits
  3. For each commit's blobs:
     GET /api/sync/myproject/blob/{hash}   → store locally
  4. Write .olympusrepo/config.json with origin URL
  5. Write files to working tree
```

### Offer Flow (Athens → Olympus)

```
olympusrepo offer -m "reason"
  1. Collect pending local commits
  2. POST /api/sync/myproject/offer
     Body: {commits, blobs, message}
  3. Canonical creates staging realm
  4. Zeus/Olympian reviews → promotes → canonical tree updated
```

### Anonymous Offerings

Drive-by contributions from unauthenticated users on public repos:

```
GET  /repo/{name}/edit/{branch}/{path}    → edit form (open to anon on public repos)
POST /api/repos/{name}/offer-anon         → submit with anon_name, anon_email
GET  /offering/{token}                    → bookmark page to check status
```

Anti-abuse: honeypot field, per-IP hourly rate limit (5), 50KB content cap, email regex validation.

---

## OlympusRelay

Decentralized instance discovery using Ed25519 cryptographic identity.

### Identity

Each instance generates an Ed25519 keypair on first run. The `instance_id` is the hex-encoded public key (64 chars). Stored at `.olympusrepo/identity`.

### Protocol

```
POST /relay/register          → signed heartbeat registration (every 5 min)
GET  /relay/find/{id}         → lookup instance by public key
POST /relay/punch             → NAT hole-punch coordination
GET  /relay/list              → all registered instances (no IPs exposed)
POST /relay/gossip            → relay-to-relay sync (depth-1 only)
GET  /relay/peers             → known peer relays
GET  /relay/health            → liveness check
```

### Resolution

```
olympusrepo clone olympus://a3f7b2c1.../myproject
  1. Query known relays for instance_id
  2. Relay returns IP:port
  3. Verify via signed challenge (prevent relay MITM)
  4. Substitute http://ip:port and proceed normally
```

Relay is an enhancement, not a requirement. Direct IP always works as fallback. The relay service (`relay/`) is ~300 lines of Python with zero database dependencies (SQLite + in-memory).

---

## Web Application

### Architecture

- **Framework:** FastAPI with Jinja2 templates
- **Entry point:** `olympusrepo/web/app.py` (~5000 lines, single router)
- **Git endpoints:** `olympusrepo/web/git_protocol.py` (mounted via `include_router`)
- **Templates:** `templates/` (40+ Jinja2 HTML files)
- **Static files:** `static/` (background images, served at `/static/`)
- **Session auth:** 64-char hex token in `httponly` + `samesite=strict` cookie
- **Real-time:** WebSocket at `/ws/mana/{channel}` for live discussion

### Request Flow

```
HTTP request → FastAPI router
  → get_db(request)          # Connection from pool
  → get_current_user(request, conn)  # Session cookie → user dict (or None for anon)
  → Route handler
      → core/* function calls
      → TemplateResponse or JSONResponse
  → Connection returned to pool
```

### Template Rendering

All templates receive plain dicts, never ORM objects. Base template (`base.html`) provides nav, notification badge, message count, role badges, and the mythological background system.

---

## CLI

`olympusrepo/cli.py` — argparse-based, ~900 lines.

### Commands

| Command | What it does |
|---------|-------------|
| `init <name>` | Create repo in DB + initialize `.olympusrepo/` |
| `add [files...]` | Scan tree, hash files, write to `index.json` |
| `commit -m "msg"` | Detect staged changes, store blobs, write commit |
| `status` | Show staged + unstaged changes vs committed baseline |
| `log` | Show commit history for current branch |
| `diff [file]` | Unified diff of working tree vs committed |
| `branch [name]` | List or create branches |
| `switch <branch>` | Change HEAD file |
| `resolve <file>` | Mark conflict resolved (remove from conflict list) |
| `clone <url> [dest]` | Full repo clone via sync protocol or `olympus://` |
| `pull [--remote]` | Pull updates from canonical |
| `offer [-m "reason"]` | Submit local commits for review on canonical |
| `remote add/list/remove` | Manage federation remotes |
| `mana [-m "msg"]` | Post or list design discussion |
| `fsck` | Check repo integrity (missing blobs, orphans, null refs) |
| `prune [--force]` | Delete orphaned blobs (dry-run by default) |
| `import-git <path> <name>` | Import a git repo via CLI |
| `user-create <u> <pw>` | Create user (Zeus only) |
| `delete-repo <name>` | Delete repository (Zeus only) |

### Local State

CLI state lives in `.olympusrepo/` at the repo working directory root. The `OLYMPUSREPO_OBJECTS_DIR` env var controls where blobs are stored — must be shared between CLI and server.

---

## Authentication & Security

### Password Storage

bcrypt via pgcrypto (`repo_create_user` SQL function). Minimum 8 characters enforced in `db.py`.

### Sessions

64-char hex tokens from `gen_random_bytes(32)`. Stored in `repo_sessions` with expiry. Cookies: `httponly=True`, `samesite=strict`, `secure` when `OLYMPUSREPO_COOKIE_SECURE=1`.

### Personal Access Tokens (PATs)

Prefix: `olyp_` + 256-bit base64url token (~48 chars). Hashed with bcrypt before storage. Scopes: `git:read`, `git:write`, `api:read`, `api:write`. Optional expiry. Managed at `/account/tokens`.

### Row-Level Security

`repo_messages` (private mana/DMs) uses Postgres RLS policies keyed on `app.current_user_id` session variable. Set via `db.set_session_user()`.

### Git Remote Credentials

Encrypted at rest via `pgp_sym_encrypt` keyed off `repo_server_config['git_creds_key']` (random per install). Decrypted only when needed for push/pull operations.

### SQL Injection Prevention

Every query uses `%s` parameterization. No string concatenation in SQL. No exceptions. This is enforced by code review convention.

---

## Role Hierarchy

| Role | Power Level | Capabilities |
|------|-------------|-------------|
| **Zeus** | Instance owner | Everything. Canonical tree, permissions, visibility, user management, server config. |
| **Olympian** | Senior dev | Review and promote offerings to canonical tree (scoped by Zeus). |
| **Titan** | Regular contributor | Commit to personal staging realm. |
| **Mortal** | Junior/guest | Limited staging access. |
| **Prometheus** | Experimental | Isolated sandbox for risky changes. |
| **Hermes** | Hotfix | Emergency fast-path patches. |

Stored as `CHECK (role IN (...))` on `repo_users.role`. Extensible via migration.

---

## API Surface

### Authentication
```
POST /api/auth/login                        Login with username/password
POST /api/auth/signup                       Create account (if registration open)
POST /api/auth/logout                       Destroy session
GET  /api/auth/me                           Current user info
POST /api/auth/reset-password               Password reset flow
```

### Repositories
```
GET    /api/repos                           List repos (filtered by access)
GET    /api/repos/{name}                    Repo metadata
POST   /api/repos                           Create repo
DELETE /api/repos/{name}                    Delete repo (Zeus only)
POST   /api/repos/{name}/settings           Update settings
POST   /api/repos/{name}/branches           Create branch
GET    /api/repos/{name}/branches           List branches
GET    /api/repos/{name}/log                Commit log
```

### File Operations
```
POST /api/repos/{name}/upload               Upload file (web commit)
GET  /api/repos/{name}/commits/{hash}/diff  Commit diff
```

### Staging & Promotion
```
GET  /api/repos/{name}/staging/{id}/diff    Staging diff
POST /api/repos/{name}/promote/{id}         Promote staging to canonical
POST /api/repos/{name}/offer-anon           Anonymous contribution
```

### Git Remotes
```
POST   /api/repos/{name}/remotes                        Add remote
DELETE /api/repos/{name}/remotes/{remote}                Remove remote
POST   /api/repos/{name}/remotes/{remote}/push           Push to remote
POST   /api/repos/{name}/remotes/{remote}/pull           Pull from remote
POST   /api/repos/{name}/remotes/{remote}/test           Test connection
```

### Federation Sync
```
GET  /api/sync/{name}/info                  Repo info for clone
GET  /api/sync/{name}/commits               Commit list (paginated)
GET  /api/sync/{name}/blob/{hash}           Raw blob download
POST /api/sync/{name}/offer                 Submit offer from remote instance
```

### Smart-HTTP (Git Protocol)
```
GET  /<repo>.git/info/refs?service=...      Ref advertisement
POST /<repo>.git/git-upload-pack            Clone/fetch pack
POST /<repo>.git/git-receive-pack           Push receive
```

### Discussion (Mana)
```
POST /api/repos/{name}/mana                 Post mana
POST /api/repos/{name}/mana/{id}/reply      Reply to mana
POST /api/repos/{name}/comments             Inline code comment
WS   /ws/mana/{channel}                     Live WebSocket updates
```

### Issues
```
POST /api/repos/{name}/issues               Create issue
POST /api/repos/{name}/issues/{n}/comments  Comment on issue
```

### Notifications
```
GET  /api/notifications                     List notifications
POST /api/notifications/{id}/read           Mark read
POST /api/notifications/read-all            Mark all read
```

### Personal Access Tokens
```
POST   /api/account/tokens                  Create PAT
DELETE /api/account/tokens/{id}             Revoke PAT
```

### Messages
```
POST /api/messages                          Send DM
GET  /api/messages/unread-count             Unread badge count
```

### Admin
```
POST /api/admin/users                       Create user
POST /api/admin/config/set                  Update server config
```

---

## Configuration

All config lives in `.env`, loaded at startup:

```bash
# Database
OLYMPUSREPO_DB_NAME=olympusrepo
OLYMPUSREPO_DB_USER=olympus
OLYMPUSREPO_DB_PASS=yourpassword
OLYMPUSREPO_DB_HOST=127.0.0.1
OLYMPUSREPO_DB_PORT=5432

# Server
OLYMPUSREPO_PORT=8000
OLYMPUSREPO_PUBLIC_URL=http://your.domain:8000

# Storage — CLI and server MUST share these paths
OLYMPUSREPO_OBJECTS_DIR=./objects
OLYMPUSREPO_MIRRORS_DIR=./mirrors
OLYMPUSREPO_GATEWAYS_ROOT=./gateways

# Security
OLYMPUSREPO_COOKIE_SECURE=0          # Set to 1 behind HTTPS

# Import guardrails (0 = unlimited)
OLYMPUSREPO_IMPORT_MAX_COMMITS=0
OLYMPUSREPO_IMPORT_MAX_BYTES=0

# Relay
OLYMPUSREPO_RELAY_ENABLED=1
OLYMPUSREPO_INSTANCE_NAME=My Olympus
OLYMPUSREPO_RELAYS=https://relay1.olympus.community
```

---

## SQL Migrations

Run in numeric order. Each is idempotent (uses `IF NOT EXISTS`, `ON CONFLICT DO NOTHING`, etc.):

| # | File | Purpose |
|---|------|---------|
| 001 | extensions.sql | `CREATE EXTENSION IF NOT EXISTS pgcrypto` |
| 002 | tables.sql | Core 17-table schema |
| 003 | indexes.sql | Performance indexes |
| 004 | rls.sql | Row-level security on repo_messages |
| 005 | functions.sql | `repo_create_user`, `repo_verify_password`, `repo_create_session`, `repo_validate_session` |
| 006 | defaults.sql | Default `repo_server_config` rows |
| 007 | seed.sql | Default zeus/changeme account |
| 008 | notifications.sql | `repo_notifications` table |
| 009 | password_reset.sql | `repo_password_reset_tokens` table |
| 010 | fix_fk_cascades.sql | Fix FK constraints that blocked repo deletion |
| 011 | file_revisions.sql | `repo_file_revisions` table |
| 012 | mana_threads.sql | Threading columns on `repo_messages` |
| 013 | issues.sql | `repo_issues`, `repo_issue_comments`, `repo_issue_attachments` |
| 014 | connector.sql | `repo_remotes`, `repo_sync_state`, federation offer tables |
| 015 | git_import.sql | `is_imported`, `original_sha`, `author_tz_offset`, etc. on commits |
| 016 | git_push.sql | `repo_git_remotes`, `repo_git_push_log`, `repo_git_pull_log`, `repo_pats` |
| 017 | phase4_and_tightening.sql | `file_mode`, `gpg_signature`, `dangling_parents` view |
| 018 | anon_offerings.sql | Anon offering columns on staging, `repo_anon_rate_log` |
| 019 | repair_v2_upload.sql | Fix byte_offset handling in v2 web upload |

---

## Extension Points

### Adding a new role
Add to the CHECK constraint on `repo_users.role` via a new migration. Role logic is checked in Python (`app.py` route guards), not enforced at the DB level beyond the CHECK.

### Adding a new CLI command
Add a `cmd_<name>` function in `cli.py` and register it in the argparse subparser block at the bottom of the file.

### Adding a new API endpoint
Add a route function in `web/app.py`. Follow the pattern: validate input, call `core/*` function, return response. Keep route handlers thin.

### Adding a new table
Create a new `sql/0XX_<name>.sql` migration. Use `IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for idempotency.

### Custom ignore patterns
Add patterns to `.olympusignore` at repo root. Syntax is fnmatch glob, one per line, `#` for comments.

---

## Known Constraints

1. **`app.py` is a single ~5000-line file.** All web routes live here. It works but is large. The convention is to keep business logic in `core/` and routes thin.

2. **Materialize is O(chain length).** Walking the parent chain for deeply-nested histories can be slow. Imported commits anchor the chain (full snapshots), keeping walk length manageable for imported repos.

3. **Smart-HTTP is buffered, not streaming.** Full request/response bodies are held in memory. The 10 GiB push limit exists for this reason. Large repos work; adversarial payloads are bounded.

4. **No concurrent push safety.** Two simultaneous pushes to the same repo can race on ref updates. Single-writer assumption for now.

5. **Gateway is rebuilt, not incrementally updated.** `ensure_gateway_synced()` re-runs `git fast-import` from scratch when the gateway is stale. Fast for most repos; slow for very large ones. Suitable since it's a one-time operation per push/pull cycle.

6. **No pack files.** Objects are loose on disk (fan-out by first 2 hex chars). Good enough for millions of objects. Packing would improve disk usage and clone speed for very large repos.

7. **Single PostgreSQL instance.** No read replicas, no sharding. Postgres can handle millions of rows in these tables without issue. Scale up, not out.

---

*Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering. MIT License.*
