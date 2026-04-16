# OlympusRepo — Setup & Installation Guide

This guide covers installation on **WSL2 (Ubuntu)**, **native Linux (Debian/Ubuntu)**, and **macOS**.

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.10+ | `python3 --version` |
| PostgreSQL | 14+ | `psql --version` |
| diff3 | any | Part of `diffutils`, needed for three-way merges |
| git | any | To clone the repo |

---

## 1. PostgreSQL Setup

### WSL2 / Ubuntu / Debian

```bash
# Install if not already present
sudo apt update
sudo apt install postgresql postgresql-contrib diffutils

# Start the service
sudo service postgresql start

# Verify it's running
sudo service postgresql status
```

PostgreSQL on WSL2 does not start automatically on boot. You need to run `sudo service postgresql start` each time you open a new WSL session, or add it to your `~/.bashrc`:

```bash
echo "sudo service postgresql start > /dev/null 2>&1" >> ~/.bashrc
```

### macOS

```bash
brew install postgresql@16 diffutils
brew services start postgresql@16
```

---

## 2. Create the Database User and Database

By default OlympusRepo connects as a user called `olympus`. Create that user and the database:

```bash
# Switch to the postgres superuser
sudo -u postgres psql

# Inside psql:
CREATE USER olympus WITH PASSWORD 'your_password_here';
CREATE DATABASE olympusrepo OWNER olympus;
GRANT ALL PRIVILEGES ON DATABASE olympusrepo TO olympus;
\q
```

Then set your environment variables so OlympusRepo knows how to connect. Add these to your `~/.bashrc` (or `~/.zshrc` on macOS):

```bash
export OLYMPUSREPO_DB_NAME=olympusrepo
export OLYMPUSREPO_DB_USER=olympus
export OLYMPUSREPO_DB_PASS=your_password_here
export OLYMPUSREPO_DB_HOST=127.0.0.1
export OLYMPUSREPO_DB_PORT=5432
```

Reload your shell:

```bash
source ~/.bashrc
```

> **Alternative:** If you just want to get running quickly, you can connect as your system user. Skip the `CREATE USER` step and set `OLYMPUSREPO_DB_USER` to your Linux username, leaving `OLYMPUSREPO_DB_PASS` empty. PostgreSQL's default `peer` auth will handle it.

---

## 3. Clone the Repository

```bash
git clone https://github.com/AiLang-Author/OlympusRepo.git
cd OlympusRepo
```

---

## 4. Run Database Migrations

```bash
for f in sql/0*.sql; do echo "Running $f..."; psql olympusrepo < "$f"; done
```

Expected output ends with:

```
Running sql/007_seed.sql...
NOTICE:  Default zeus account created. CHANGE THE PASSWORD IMMEDIATELY.
DO
```

If you see errors on `001_extensions.sql`, your database user may not have superuser privileges (needed for `CREATE EXTENSION`). Fix with:

```bash
sudo -u postgres psql -c "ALTER USER olympus SUPERUSER;"
```

Then re-run the migrations.

---

## 5. Python Environment

Always use a virtual environment. Never install into the system Python.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

You should see `(.venv)` at the start of your prompt. Verify the CLI is installed:

```bash
olympusrepo --help
```

> **Every time you open a new terminal** you need to reactivate the venv:
> ```bash
> cd OlympusRepo
> source .venv/bin/activate
> ```
> Or add an alias to your `~/.bashrc`:
> ```bash
> alias olympus='cd /path/to/OlympusRepo && source .venv/bin/activate'
> ```

---

## 6. Start the Web Server

```bash
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open your browser at **http://localhost:8000**

The `--reload` flag restarts the server automatically when you edit Python files. Drop it in production.

### WSL2 note

If you're running WSL2 and can't reach `localhost:8000` from your Windows browser, find your WSL IP:

```bash
hostname -I
```

Use that IP in your browser instead of `localhost`.

---

## 7. First Login

The seed script created a default admin account:

- **Username:** `zeus`
- **Password:** `changeme`

Log in at http://localhost:8000/login and change the password immediately via the admin panel, or from psql:

```bash
psql olympusrepo -c "SELECT repo_create_user('zeus2', 'strongpassword', 'zeus');"
```

---

## 8. Create Your First Repository

```bash
# Make sure your venv is active
source .venv/bin/activate

# Create a user for yourself
olympusrepo user-create yourname yourpassword --role titan

# Initialize a new repo
olympusrepo init myproject --user yourname
cd myproject

# Add some files and commit
echo "# My Project" > README.md
olympusrepo add .
olympusrepo commit -m "Initial commit"
olympusrepo log
```

---

## 9. Production Checklist

If you're exposing this to the internet rather than running locally:

- [ ] Set a strong password on the `zeus` account
- [ ] Set `OLYMPUSREPO_COOKIE_SECURE=1` (requires HTTPS)
- [ ] Put a reverse proxy (nginx, Caddy) in front of uvicorn
- [ ] Don't run with `--reload`
- [ ] Change `registration_policy` to `invite_only` or `approval_required`:
  ```bash
  psql olympusrepo -c "UPDATE repo_server_config SET value='invite_only' WHERE key='registration_policy';"
  ```
- [ ] Back up your PostgreSQL database and your object store (`packs/` and `.olympusrepo/objects/`)

---

## Troubleshooting

**`psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed`**
PostgreSQL isn't running. Start it:
```bash
sudo service postgresql start   # WSL2/Linux
brew services start postgresql  # macOS
```

**`createdb: error: database "olympusrepo" already exists`**
The database is already there. Skip `createdb` and just run the migrations.

**`error: externally-managed-environment` when running pip**
You're trying to install into the system Python. Use the venv:
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

**`ModuleNotFoundError: No module named 'olympusrepo'`**
Your venv isn't active. Run `source .venv/bin/activate` first.

**`diff3: command not found` when merging**
Install diffutils:
```bash
sudo apt install diffutils    # WSL2/Linux
brew install diffutils        # macOS
```

**Port 8000 already in use**
Pick a different port: `uvicorn olympusrepo.web.app:app --port 8080`

---

## Quick Reference

```bash
# Start postgres (WSL2)
sudo service postgresql start

# Activate venv
source .venv/bin/activate

# Start web server
uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000 --reload

# CLI commands
olympusrepo init <name>
olympusrepo add .
olympusrepo commit -m "message"
olympusrepo status
olympusrepo log
olympusrepo branch <name>
olympusrepo switch <name>
olympusrepo diff
olympusrepo resolve <file>
olympusrepo user-create <username> <password> --role titan
```