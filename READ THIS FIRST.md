# OlympusRepo — Setup Guide
*2 Paws Machine and Engineering — v0.5.0*

---

## The Short Version

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo
bash setup.sh
```

Answer the prompts. When it finishes, start the server and open your browser. That's it.

---

## Prerequisites

OlympusRepo needs three things on your machine before setup.sh runs:

- **Python 3.10+** — `python3 --version`
- **PostgreSQL 14+** — `psql --version`
- **git** — `git --version`

setup.sh checks for all three and will attempt to install missing ones automatically on Ubuntu/Debian/WSL2 and macOS (via Homebrew).

### WSL2 (Windows)

WSL2 is fully supported and the recommended path on Windows. PostgreSQL does not start automatically — setup.sh will start it for you and offer to auto-start it on every terminal open.

If you don't have WSL2 yet:
1. Open PowerShell as Administrator
2. `wsl --install`
3. Reboot, open Ubuntu from the Start menu
4. Come back here

### macOS

Homebrew is required. If you don't have it: https://brew.sh

PostgreSQL via Homebrew: `brew install postgresql@16`

### Linux (native)

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib python3 python3-venv python3-pip git diffutils
sudo service postgresql start
```

---

## Installation

### Step 1 — Clone the repo

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo
```

### Step 2 — Run setup.sh

```bash
bash setup.sh
```

setup.sh will walk you through:

1. **Mode** — Personal (local only), Team (canonical instance), or Contributor (offer changes to someone else)
2. **Prerequisites** — checks Python, PostgreSQL, git
3. **Database** — creates the `olympus` DB user and `olympusrepo` database
4. **Migrations** — runs all 14 SQL migration files in order
5. **Python environment** — creates `.venv`, installs all dependencies
6. **Zeus account** — creates your admin account (pick any username, not "zeus")
7. **Relay** — optional decentralized discovery setup (enables `olympus://` URIs)
8. **Network** — Tailscale, static IP/domain, Tor, or manual
9. **Environment** — writes `.env`, adds shell alias, adds env sourcing to `.bashrc`/`.zshrc`

When it finishes you'll see your instance alias (e.g. `olympus-olympusrepo`) and the start command.

### Step 3 — Start the server

```bash
source ~/.bashrc          # or open a new terminal
olympus-olympusrepo       # activates venv + loads .env

uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` and log in with the Zeus username and password you set.

---

## First Steps After Login

### Change the default seed password

The seed account `zeus` / `changeme` is deactivated by setup.sh. Your Zeus account is the one you created. You're already set.

### Create your first repository

In the browser: click **New Repository** from the dashboard.

Or via CLI:
```bash
mkdir myproject && cd myproject
olympusrepo init myproject --user YOURUSERNAME
echo "# My Project" > README.md
olympusrepo add .
olympusrepo commit -m "initial commit"
```

### Share with contributors

Give them your instance URL or your `olympus://` URI (visible on the Relay Config page at `/zeus/relay`).

---

## Two-Instance Setup (Canonical + Contributor)

This is the core workflow — one canonical instance that accepts offers, one contributor instance that clones and offers.

### On the canonical machine (already set up above)

Make sure the server is running and accessible. Note your instance URL or `olympus://` URI from `/zeus/relay`.

### On the contributor machine

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo
bash setup.sh
# Choose mode: 3 (Contributor)
# Enter the canonical URL when prompted
```

Then clone a repo and start working:

```bash
# Via direct URL:
olympusrepo clone http://CANONICAL_IP:8000/repo/myproject myproject

# Via relay URI (if canonical has relay configured):
olympusrepo clone olympus://INSTANCE_ID/myproject myproject

cd myproject
# make changes
olympusrepo add .
olympusrepo commit -m "my fix"
olympusrepo offer -m "here is why this should be accepted"
```

### On canonical — review and promote

Open `http://localhost:8000`, go to the repo, click the **Staging** tab. The offer appears. Click **Review** to see the diff, then **Promote** to merge it.

---

## OlympusRelay — Decentralized Discovery

The relay lets contributors find your instance without knowing your IP address. Your instance gets a permanent cryptographic identity (`instance_id`) and registers with relay nodes. Contributors clone with `olympus://INSTANCE_ID/repo` and the relay resolves it to your current IP.

### Running a local relay (recommended for Team mode)

setup.sh offers to install and configure a relay automatically. If you said yes, a `start-relay.sh` was written for you:

```bash
./start-relay.sh
```

The relay runs on port 9000 by default. Configure it at `/zeus/relay` in the browser.

### Sharing your olympus:// URI

1. Start your relay: `./start-relay.sh`
2. Start your server
3. Go to `http://localhost:8000/zeus/relay`
4. Copy the **Clone URI** — it looks like `olympus://428d8f944604.../repo-name`
5. Share that URI with contributors

The URI works as long as your instance is registered with at least one reachable relay. No static IP needed. No DNS needed.

### Running a public relay node

Anyone can run a relay. It has zero database dependencies:

```bash
pip install -e relay/
olympusrelay --port 9000 --peers relay1.olympus.community
```

Add your relay URL to `OLYMPUSREPO_RELAYS` in `.env` on any instance you want to register with it.

---

## Environment Variables

All configuration lives in `.env` in your OlympusRepo directory. setup.sh writes this for you. Key variables:

```bash
# Database
OLYMPUSREPO_DB_NAME=olympusrepo
OLYMPUSREPO_DB_USER=olympus
OLYMPUSREPO_DB_PASS=yourpassword
OLYMPUSREPO_DB_HOST=127.0.0.1
OLYMPUSREPO_DB_PORT=5432

# Server
OLYMPUSREPO_PORT=8000
OLYMPUSREPO_PUBLIC_URL=http://your.domain.or.ip:8000

# Object store — CLI and server MUST share this path
OLYMPUSREPO_OBJECTS_DIR=/path/to/olympusrepo/objects

# Security — set to 1 if behind HTTPS reverse proxy
OLYMPUSREPO_COOKIE_SECURE=0

# Relay
OLYMPUSREPO_RELAY_ENABLED=1
OLYMPUSREPO_INSTANCE_NAME=My Olympus
OLYMPUSREPO_RELAYS=http://localhost:9000
```

---

## CLI Quick Reference

```bash
olympusrepo init <name>              Create a new repository
olympusrepo add [files...]           Stage files
olympusrepo commit -m "message"      Commit
olympusrepo status                   Show working tree status
olympusrepo log                      Commit history
olympusrepo diff [file]              Show changes
olympusrepo branch [name]            List or create branches
olympusrepo switch <branch>          Switch branches
olympusrepo resolve <file>           Mark conflict resolved
olympusrepo clone <url> [dest]       Clone from remote (http:// or olympus://)
olympusrepo pull [--remote origin]   Pull from canonical
olympusrepo offer [-m "reason"]      Offer commits for review
olympusrepo remote add <n> <url>     Add a remote
olympusrepo fsck                     Check repository integrity
olympusrepo prune                    Remove orphaned blobs (dry-run)
olympusrepo prune --force            Actually delete orphaned blobs
olympusrepo import-git <path> <name> Import a git repository
olympusrepo user-create <u> <pw>     Create a user (Zeus only)
olympusrepo delete-repo <name>       Delete a repository (Zeus only)
```

---

## Maintenance

### Check integrity

```bash
cd myrepo
olympusrepo fsck
```

Reports missing blobs, malformed changesets, and orphaned objects.

### Clean up orphaned blobs

```bash
olympusrepo prune          # dry-run — shows what would be deleted
olympusrepo prune --force  # actually deletes
```

### Backup

Back up two things:
1. PostgreSQL database: `pg_dump olympusrepo > backup.sql`
2. Object store: `tar -czf objects_backup.tar.gz objects/`

Restore: restore the DB, restore the objects directory, done.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'olympusrepo'`**
Your venv isn't active. Run your instance alias (e.g. `olympus-olympusrepo`) or `source .venv/bin/activate`.

**`connection refused` on port 8000**
The server isn't running. Run `uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000`.

**`FATAL: password authentication failed`**
Wrong DB password. Check `OLYMPUSREPO_DB_PASS` in `.env`.

**PostgreSQL not running (WSL2)**
`sudo service postgresql start`

**`olympusrepo fsck` reports missing blobs**
Run `olympusrepo pull` to re-fetch from canonical. If you are canonical, the blobs are gone — restore from backup.

**Relay shows Unreachable**
The relay isn't running. Start it with `./start-relay.sh` or `olympusrelay --port 9000`.

**`olympus://` URI clone fails**
Make sure `OLYMPUSREPO_RELAYS` in `.env` points to a running relay and your instance is registered. Check `/zeus/relay` in the browser.

**Merge conflicts after pull**
OlympusRepo does not auto-merge. Edit the conflicted files, remove conflict markers, then run `olympusrepo resolve <file>`.

---

## License

MIT. Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering.
