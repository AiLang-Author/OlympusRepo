# OlympusRepo — User Manual

## What Is OlympusRepo?

OlympusRepo is a version control system built around a simple idea: not everyone on a team should have the same power over the codebase. Some people write code. Some people review it. One person owns it. The system enforces that hierarchy rather than leaving it to convention and hoping everyone follows the rules.

It works like this: you commit your changes to your own personal staging realm. A reviewer looks at your work and promotes it to the canonical tree when it's ready. Every decision, every discussion, every promotion is recorded permanently. Nothing disappears.

---

## The Hierarchy

Every user has a role. Your role determines what you can do.

| Role | Badge Color | What You Can Do |
|------|-------------|-----------------|
| **Zeus** | Gold | Everything. Owns the canonical tree. Controls who can do what. Creates repositories. |
| **Olympian** | Purple | Reviews and promotes work from staging to canonical. Scoped by Zeus. |
| **Titan** | Blue | Commits to personal staging realm. The standard contributor role. |
| **Mortal** | Gray | Limited staging access. Good for guests and junior contributors. |
| **Prometheus** | Red | Isolated sandbox for experimental or risky changes. |
| **Hermes** | Green | Emergency fast-path patches. |

Your role badge appears in the top-right navigation bar when you're logged in.

---

## Logging In

Go to your OlympusRepo instance and click **Login** in the top-right corner.

Enter your username and password. If you don't have an account, click **Sign up** on the login page. New accounts are created as **Mortal** by default. Zeus can promote you to a higher role after you sign up.

---

## The Web Interface

### Repositories Page (`/`)

The main page lists all repositories you have access to. Each card shows the repo name, visibility (public / private / internal), description, and when it was last updated. Click any repo to open it.

If you are Zeus, a **New Repository** button appears in the top right. *(Note: the creation form is coming in the next version — for now, create repos via the CLI.)*

### Repository Browser (`/repo/<name>`)

Opening a repository shows four tabs depending on your role:

**Files** — Lists the files in the repository at the current branch. *(File tree loading from commit objects is coming in the next version. For now, use the CLI to browse files.)*

**Commits** — Full commit history in reverse order. Shows revision number, short hash, author, date, and commit message.

**Mana** — All design discussion attached to this repository. See the Mana section below.

**Staging** — Visible to Zeus and Olympians only. Shows all active staging realms for this repo with pending changes waiting for review.

### Branch Selector

Above the tabs, buttons show all available branches. Click one to switch the view to that branch.

### The Throne (`/zeus`)

Only Zeus and Olympians can access The Throne. It shows:

- **Active Offerings** — All staging realms across all repos with pending changes. Each row shows the contributor, their role, their branch name, how many files have changed, and a **Promote** button.
- **Audit Log** — Every write action ever performed on the instance: logins, commits, promotions, user creation, config changes. Timestamped, attributed, permanent.
- **Quick Actions** — Links to user management, server config, and repo creation.

---

## Mana — Design Discussion

Mana is OlympusRepo's name for discussion that lives permanently attached to the code. It is not a chat room. It is a record.

When you write mana, you can attach it to a specific context:

| Context Type | Use For |
|---|---|
| **General** | Repo-wide discussion, announcements |
| **File** | Notes about a specific file (enter the file path) |
| **Commit** | Commentary on a specific commit (enter the commit hash) |
| **Staging** | Discussion about a staging realm under review (enter the branch name) |

To send mana, go to the **Mana** tab in any repository, write your message, select the context type, optionally fill in the context ID, and click **Send**.

Mana is permanent. There is no delete. That is intentional — design decisions should be traceable.

---

## The CLI

The command-line tool is how you actually work with repositories day to day. Make sure your virtual environment is active before using it:

```bash
source .venv/bin/activate
```

### Creating a Repository

Only Zeus can create repositories:

```bash
olympusrepo init my-project --user zeus --visibility public
```

This creates the repository in the database and initializes a local working directory at `./my-project`.

Visibility options:
- `public` — anyone can read it, including anonymous users
- `internal` — any logged-in user can read it
- `private` — only Zeus and explicitly granted users can read it

### Working in a Repository

```bash
cd my-project
```

All CLI commands run from inside the repository directory (or any subdirectory — it walks up to find `.olympusrepo/`).

### Checking Status

```bash
olympusrepo status
```

Shows which files have been modified, added, or deleted since your last commit.

```
On branch: main

Modified:
  src/auth.py

New files:
  src/token.py

3 change(s) total.
```

### Staging Files

```bash
olympusrepo add .              # stage everything
olympusrepo add src/auth.py   # stage a specific file
olympusrepo add src/           # stage a directory
```

This records the current state of those files in your local index. You need to `add` files before committing.

### Committing

```bash
olympusrepo commit -m "Add token-based auth to login flow"
```

This creates a commit in your **staging realm** — not the canonical tree. Your changes are saved and visible to Olympians for review, but they are not in `main` yet.

Output:
```
Committed rev 7  a3f2c1b8d901
  3 file(s) changed
  Add token-based auth to login flow
```

The revision number is global and sequential across the entire instance.

### Viewing History

```bash
olympusrepo log                    # last 20 commits
olympusrepo log --limit 50         # last 50 commits
olympusrepo log --path src/auth.py # commits that touched a specific file
```

### Viewing Differences

```bash
olympusrepo diff                   # diff all modified files
olympusrepo diff src/auth.py       # diff a specific file
```

Shows a standard unified diff with `+` for added lines and `-` for removed lines.

### Branches

```bash
olympusrepo branch                 # list all branches (* = current)
olympusrepo branch feature/login   # create a new branch from current
olympusrepo switch feature/login   # switch to a branch
```

Branches are lightweight — they're just pointers to commits. Creating a branch does not copy files.

### Resolving Merge Conflicts

If a three-way merge produces conflicts, the affected files will contain conflict markers:

```
<<<<<<< OURS
your version of the code
=======
their version of the code
>>>>>>> THEIRS
```

Edit the file to resolve the conflict — remove the markers and keep the correct code. Then tell OlympusRepo you've resolved it:

```bash
olympusrepo resolve src/auth.py
```

This checks that all conflict markers are gone before allowing you to commit. If any remain, it tells you how many and where.

### Creating Users (Zeus Only)

```bash
olympusrepo user-create alice strongpassword --role titan
olympusrepo user-create bob anotherpassword --role olympian --full-name "Bob Smith"
```

Role options: `zeus`, `olympian`, `titan`, `mortal`, `prometheus`, `hermes`

Passwords must be at least 8 characters.

---

## The Promotion Workflow

This is the core of how OlympusRepo works.

**From the Titan's perspective:**
1. Write your code
2. `olympusrepo add .`
3. `olympusrepo commit -m "what you did and why"`
4. Your work is now in your staging realm, visible to reviewers

**From the Olympian's perspective:**
1. Go to the repo in the web UI
2. Click the **Staging** tab
3. Review the contributor's changes — how many files, when it was updated
4. Optionally discuss it in **Mana**, referencing the staging branch
5. Click **Promote**
6. Enter promotion notes (what you reviewed, what you approved)
7. The changes become a new commit in the canonical tree with a new global revision number

**From Zeus's perspective:**
1. The Throne shows all active offerings across all repos
2. Zeus can promote directly from The Throne without going into individual repos
3. The audit log records who promoted what, when, and with what notes

---

## Ignoring Files

Create a `.olympusignore` file in your repository root. One pattern per line. Supports glob wildcards. Lines starting with `#` are comments.

```
# .olympusignore
build/
dist/
*.log
*.env
secrets/
coverage/
.cache/
```

OlympusRepo always ignores these by default regardless of your `.olympusignore`:
`.git`, `.olympusrepo`, `__pycache__`, `*.pyc`, `node_modules`, `.venv`, `.DS_Store`, `Thumbs.db`

---

## What's Not Done Yet

OlympusRepo is alpha software. These features are designed and coming but not implemented yet:

- **File tree browser** — Viewing individual files in the web UI
- **New repository form** — Creating repos from the web UI (use the CLI for now)
- **Clone / download** — Downloading a repo as a tarball
- **User management page** — Managing users from the web UI (use the CLI for now)
- **Server config page** — Changing instance settings from the web UI
- **WebSocket mana** — Real-time discussion updates (page reload required for now)
- **Full staging workflow in web UI** — The promotion button works, but the detailed diff view per staging realm is coming

---

## Quick Reference Card

```
# Every session
sudo service postgresql start    # WSL2 only
cd /path/to/OlympusRepo
source .venv/bin/activate
OLYMPUSREPO_DB_PASS=yourpassword uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port 8000

# Daily work
olympusrepo status               # what changed?
olympusrepo add .                # stage everything
olympusrepo commit -m "message"  # commit to staging
olympusrepo log                  # view history
olympusrepo diff                 # view changes
olympusrepo branch <name>        # create branch
olympusrepo switch <name>        # switch branch

# Admin (Zeus only)
olympusrepo init <name> --user zeus
olympusrepo user-create <user> <pass> --role titan
```
