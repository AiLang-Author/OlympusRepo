# OlympusRepo CLI Reference

Complete reference for the `olympusrepo` command-line tool, plus the standard
git commands that work against the v2 smart-HTTP server.

For the workflow narrative ("how do I actually use this?") see
[USER_MANUAL.md](USER_MANUAL.md). For setup, see the project [README](../README.md).

---

## Repository Basics

| Command | Description |
|---------|-------------|
| `olympusrepo init <name>` | Create a new repository in the current directory. |
| `olympusrepo add [files...]` | Stage files for commit. Default: `.` (everything in the working tree). |
| `olympusrepo commit -m "msg"` | Commit staged changes to the repo's canonical tree (or your staging realm if you're not Zeus/Olympian on this repo). |
| `olympusrepo status` | Show working-tree status: staged, modified, untracked, conflicted. |
| `olympusrepo log [--limit N]` | Show commit history, newest first. Default limit: 50. |
| `olympusrepo diff [file]` | Show unstaged changes. Pass a filename to diff just that file. |

---

## Branching

| Command | Description |
|---------|-------------|
| `olympusrepo branch [name]` | List branches, or create one if `name` is provided. |
| `olympusrepo switch <branch>` | Switch the working tree to a different branch. |
| `olympusrepo resolve <file>` | Mark a conflicted file as resolved after editing. |

OlympusRepo deliberately does not implement `rebase`, `cherry-pick`, `reflog`, or `reset --mixed`. Linear history with a clear promotion model is the design.

---

## Olympus Federation (the native two-instance flow)

OlympusRepo's primary collaboration model: contributors `offer` changes to a canonical instance; Zeus / Olympians `promote` them.

| Command | Description |
|---------|-------------|
| `olympusrepo clone <url> [dest]` | Clone an Olympus repository. Supports `http://` and `olympus://` URIs. |
| `olympusrepo pull [--remote origin]` | Pull the latest canonical state down to your working tree. |
| `olympusrepo offer [-m "reason"]` | Send your local commits to the canonical instance as an offering for review. |
| `olympusrepo remote add <name> <url>` | Add an Olympus federation remote (typically `origin`). |
| `olympusrepo remote list` | List configured federation remotes. |
| `olympusrepo remote remove <name>` | Remove a federation remote. |

The federation flow uses the Olympus protocol, not git smart-HTTP. Use it when both ends are running OlympusRepo. For interop with anything else, see "Standard Git" below.

---

## Migration & Git Interop (v2 — NEW)

| Command | Description |
|---------|-------------|
| `olympusrepo import-git <source> <name>` | Import a full git repository — GitHub URL, GitLab URL, SSH URL, or absolute local path. Preserves SHAs, parents, author/committer identity, and timezone offsets. |
| `olympusrepo fsck` | Check repository integrity (missing blobs, dangling references, etc.). |
| `olympusrepo prune` | Show orphaned blobs (dry-run). |
| `olympusrepo prune --force` | Remove orphaned blobs from the object store. |

The web UI's `/import` page calls the same import code path with friendlier error messages and progress feedback — use whichever you prefer.

---

## Standard Git (v2 — NEW)

The v2 smart-HTTP server exposes a standard git protocol endpoint per repo. Any `git` client works against it without any olympusrepo tooling installed.

| Command | What it does |
|---------|---|
| `git clone http://yourbox:8000/<reponame>.git` | Clone an Olympus repo as a normal git working tree. No auth needed for public repos. |
| `git pull origin <branch>` | Fetch + merge updates. |
| `git fetch origin` | Fetch updates without merging. |
| `git push origin <branch>` | Push your changes back. **Requires authentication** (PAT or basic auth) — anonymous pushes are refused. |

Auth uses standard HTTP Basic. Username can be your OlympusRepo username; password can be either your OlympusRepo password OR a Personal Access Token (`olyp_…` prefix) generated from the web UI.

Stash credentials with `~/.netrc` or `git config credential.helper store` the same way you would with any other git host.

---

## Push & Pull via the Web UI (v2 — NEW)

For push/pull against external git remotes (GitHub, GitLab, Forgejo, etc.) configured per Olympus repo:

1. Open `http://yourbox:8000/repo/<name>/remotes`
2. **Bind Realm** *(/ git remote add)* — give it a name, URL, and optional credential
3. **Test Connection** *(/ ls-remote)* — confirms reachability + auth before you trigger a heavy operation
4. **Send Offering** *(/ git push)* — push the selected branch to the remote
5. **Receive Tribute** *(/ git pull)* — fetch + import any new commits

Every push/pull attempt is logged in `repo_git_push_log` / `repo_git_pull_log` and visible in the per-remote history panel. Force-push is gated behind an inline checkbox (no surprise overwrites).

Credentials are encrypted at rest with `pgp_sym_encrypt` keyed off `repo_server_config['git_creds_key']` — never written in plaintext.

---

## Personal Access Tokens (v2 — NEW)

Used for git push, API access, and any non-interactive client.

- Format: `olyp_<random_hex>` — same prefix-greppable shape as GitHub's `ghp_…`, Slack's `xoxb-…`, etc.
- Storage: bcrypt-hashed in `repo_pats`. The plaintext is shown ONCE at generation time — copy it then.
- Scopes: `git:read`, `git:write`, `api:read`, `api:write` — assignable per token.
- Use as the password field with HTTP basic auth, or as a bearer token in the `Authorization` header.

Manage from the user settings page (location varies by build — search for "Tokens" or "PATs").

---

## Administration (Zeus Only)

| Command | Description |
|---------|-------------|
| `olympusrepo delete-repo <name> [--force]` | Delete a repository. Requires `--force` for repos containing commits. |
| `olympusrepo user-create <username> <password>` | Create a new user account. |

Most admin actions live in the web UI under **The Throne** (`/zeus`): user management, audit log with filters, server config, relay configuration, repo permissions.

---

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `OLYMPUSREPO_DB_NAME` | Postgres database name | `olympusrepo` |
| `OLYMPUSREPO_DB_USER` | Postgres user | `olympus` |
| `OLYMPUSREPO_DB_PASS` | Postgres password | (no default — set this) |
| `OLYMPUSREPO_DB_HOST` | Postgres host | `127.0.0.1` |
| `OLYMPUSREPO_DB_PORT` | Postgres port | `5432` |
| `OLYMPUSREPO_PORT` | Web server port | `8000` |
| `OLYMPUSREPO_PUBLIC_URL` | Externally visible URL (used in absolute links) | `http://localhost:8000` |
| `OLYMPUSREPO_OBJECTS_DIR` | Object store path | `<install>/objects` |
| `OLYMPUSREPO_MIRRORS_DIR` | Bare git mirror cache for incremental pulls | `<install>/mirrors` |
| `OLYMPUSREPO_GATEWAYS_ROOT` | Bare gateways for the smart-HTTP server | `<install>/gateways` |
| `OLYMPUSREPO_COOKIE_SECURE` | Set to `1` if behind HTTPS reverse proxy | `0` |
| `OLYMPUSREPO_RELAY_ENABLED` | Enable OlympusRelay client | `0` |
| `OLYMPUSREPO_GIT_BIN` | Override path to git binary | `(auto-detected)` |
| `OLYMPUSREPO_GIT_TIMEOUT` | Timeout (seconds) for git subprocess calls | `300` |
| `OLYMPUSREPO_IMPORT_MAX_COMMITS` | Reject imports above this commit count | `50000` |
| `OLYMPUSREPO_IMPORT_MAX_BYTES` | Reject imports above this byte count | `2147483648` (2 GiB) |
| `OLYMPUSREPO_IMPORT_ALLOW_PRIVATE` | Set to `1` to allow imports from RFC1918/loopback hosts | `0` |

`./setup.sh` writes a complete `.env` with sensible defaults for all of these. Source it before starting the server: `set -a; source .env; set +a`.

---

## Cheatsheet

### First-time setup
```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo
./setup.sh
set -a; source .env; set +a
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000
# open http://localhost:8000, log in, create your first repo
```

### Day-to-day (native)
```bash
olympusrepo add . && olympusrepo commit -m "fix"
olympusrepo offer -m "reviewed locally, all tests pass"
```

### Day-to-day (standard git, v2)
```bash
git pull && git commit -am "fix"
git push origin main         # PAT in your credential helper
```

### Mirror an upstream
```bash
# Web UI: /import → paste https://github.com/upstream/repo.git
# Then: /repo/<name>/remotes → Bind Realm "upstream" with the same URL
# Then: Receive Tribute on a schedule (cron + curl + PAT, or manual)
```

### Round-trip back to GitHub
```bash
# /repo/<name>/remotes → Bind Realm "origin" = github.com/you/yourfork.git
# Auth: token, paste a GitHub PAT
# Click Send Offering whenever you want to push
```
