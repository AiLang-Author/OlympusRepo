# olympusrepo/web/app.py
# FastAPI web server for OlympusRepo
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering (SCSL)
#
# Run: uvicorn olympusrepo.web.app:app --reload --host 0.0.0.0 --port 8000
#
# Environment variables:
#   OLYMPUSREPO_COOKIE_SECURE=1   set secure cookie flag (for HTTPS production)

import os
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Form, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import HTTPException as FastAPIHTTPException
import asyncio
import json as json_mod

from ..core import db, repo

app = FastAPI(title="OlympusRepo", version="0.2")

from fastapi.staticfiles import StaticFiles
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

COOKIE_SECURE = os.getenv("OLYMPUSREPO_COOKIE_SECURE", "0") == "1"


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    conn = next(get_db())
    user = get_current_user(request, conn)
    conn.close()
    return templates.TemplateResponse(request, "403.html",
        {"user": user}, status_code=403)

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    conn = next(get_db())
    user = get_current_user(request, conn)
    conn.close()
    return templates.TemplateResponse(request, "404.html",
        {"user": user}, status_code=404)

# =========================================================================
# DATABASE DEPENDENCY
# =========================================================================
def get_db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# =========================================================================
# AUTH MIDDLEWARE
# =========================================================================
def get_current_user(request: Request, conn):
    """Extract authenticated user from session cookie. Returns dict or None."""
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None

    user_id = db.validate_session(conn, session_id)
    if not user_id:
        return None

    user = db.get_user(conn, user_id)
    if user:
        db.set_session_user(conn, user_id)
    return user


def require_user(request: Request, conn=Depends(get_db)):
    """Raises 401 if not authenticated."""
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def require_zeus(request: Request, conn=Depends(get_db)):
    """Require Zeus role."""
    user = require_user(request, conn)
    if user["role"] != "zeus":
        raise HTTPException(403, "The Throne is reserved for Zeus.")
    return user


class ManaConnectionManager:
    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, channel: str, ws: WebSocket):
        await ws.accept()
        if channel not in self.active:
            self.active[channel] = []
        self.active[channel].append(ws)

    def disconnect(self, channel: str, ws: WebSocket):
        if channel in self.active:
            self.active[channel].discard(ws) \
                if hasattr(self.active[channel], 'discard') \
                else self.active[channel].remove(ws)

    async def broadcast(self, channel: str, message: dict):
        if channel not in self.active:
            return
        dead = []
        for ws in self.active[channel]:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active[channel].remove(ws)

mana_manager = ManaConnectionManager()


# =========================================================================
# AUTH ROUTES
# =========================================================================
@app.post("/api/auth/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          conn=Depends(get_db)):
    user_id = db.verify_password(conn, username, password)
    if not user_id:
        raise HTTPException(401, "Invalid credentials")

    ip = request.client.host if request.client else None
    agent = request.headers.get("user-agent", "")
    session_id = db.create_session(conn, user_id, ip, agent)

    db.audit_log(conn, "login", user_id=user_id, ip=ip, commit=False)
    conn.commit()

    response = JSONResponse({"status": "ok", "user_id": user_id})
    response.set_cookie(
        "session_id", session_id,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
    )
    return response


@app.post("/api/auth/signup")
def signup(username: str = Form(...), password: str = Form(...),
           email: str = Form(None), full_name: str = Form(None),
           conn=Depends(get_db)):
    policy = db.query_scalar(conn,
        "SELECT value FROM repo_server_config WHERE key = 'registration_policy'") or "open"

    if policy == "invite_only":
        raise HTTPException(403, "Registration is invite-only. Contact the administrator.")

    try:
        user_id = db.create_user(conn, username, password, "mortal", full_name, email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        conn.rollback()
        raise HTTPException(409, "Username or email already taken")

    db.audit_log(conn, "user_create", user_id=user_id,
                 target_type="user", target_id=username, commit=False)
    conn.commit()

    return {"status": "active" if policy == "open" else "pending_approval",
            "user_id": user_id, "role": "mortal"}


@app.post("/api/auth/logout")
def logout(request: Request, conn=Depends(get_db)):
    session_id = request.cookies.get("session_id")
    if session_id:
        db.execute(conn, "DELETE FROM repo_sessions WHERE session_id = %s",
                   (session_id,))
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(
        "session_id",
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
    )
    return response


@app.get("/logout")
def logout_route(request: Request, conn=Depends(get_db)):
    session_id = request.cookies.get("session_id")
    if session_id:
        db.execute(conn, "DELETE FROM repo_sessions WHERE session_id = %s",
                   (session_id,))
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(
        "session_id",
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
    )
    return response


@app.get("/api/auth/me")
def auth_me(user=Depends(require_user)):
    return {k: user[k] for k in ("user_id", "username", "role", "full_name", "email")}


# =========================================================================
# REPO ROUTES
# =========================================================================
@app.get("/api/repos")
def list_repos_api(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    user_id = user["user_id"] if user else None
    return repo.list_repos(conn, user_id)


@app.get("/api/repos/{name}")
def get_repo_api(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404, "Repository not found")

    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403, "Access denied")

    return r


@app.get("/api/repos/{name}/log")
def repo_log_api(name: str, request: Request, limit: int = 20,
                 path: str = None, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    return repo.get_log(conn, r["repo_id"], limit=limit, path=path)


@app.get("/api/repos/{name}/branches")
def repo_branches_api(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    return repo.get_branches(conn, r["repo_id"])


@app.delete("/api/repos/{name}")
def delete_repo_api(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can delete repositories.")
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404, "Repository not found.")
    try:
        # Cascade deletes handle refs, commits, changesets, staging, messages
        db.execute(conn,
            "DELETE FROM repo_repositories WHERE repo_id = %s",
            (r["repo_id"],), commit=False)
        db.audit_log(conn, "repo_delete", user_id=user["user_id"],
                     target_type="repo", target_id=name,
                     details={"repo_id": r["repo_id"]}, commit=False)
        conn.commit()
        return {"status": "deleted", "name": name}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Could not delete repository: {e}")


@app.get("/api/notifications")
def get_notifications(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    notifs = db.query(conn, """
        SELECT notif_id, type, message, link, is_read, created_at
          FROM repo_notifications
         WHERE user_id = %s
         ORDER BY created_at DESC LIMIT 20
    """, (user["user_id"],))
    unread = sum(1 for n in notifs if not n["is_read"])
    return {"notifications": list(notifs), "unread": unread}


@app.post("/api/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    db.execute(conn,
        "UPDATE repo_notifications SET is_read = TRUE WHERE notif_id = %s AND user_id = %s",
        (notif_id, user["user_id"]))
    return {"status": "read"}


@app.post("/api/notifications/read-all")
def mark_all_read(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    db.execute(conn,
        "UPDATE repo_notifications SET is_read = TRUE WHERE user_id = %s",
        (user["user_id"],))
    return {"status": "done"}


# =========================================================================
# ZEUS DASHBOARD
# =========================================================================
@app.get("/zeus", response_class=HTMLResponse)
def zeus_dashboard(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403, "The Throne is reserved for Zeus and the Olympian council.")

    stats = {
        "total_repos": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_repositories") or 0,
        "total_users": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_users WHERE is_active = TRUE") or 0,
        "total_commits": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_commits") or 0,
        "total_promotions": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_promotions") or 0,
        "active_staging": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_staging WHERE status = 'active'") or 0,
        "commits_today": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_commits WHERE committed_at >= CURRENT_DATE") or 0,
    }

    staging = db.query(conn, """
        SELECT s.staging_id, s.branch_name, s.status, s.updated_at,
               u.username, u.role,
               COUNT(sc.change_id) as change_count
          FROM repo_staging s
          JOIN repo_users u ON u.user_id = s.user_id
          LEFT JOIN repo_staging_changes sc ON sc.staging_id = s.staging_id
         WHERE s.status = 'active'
         GROUP BY s.staging_id, s.branch_name, s.status, s.updated_at,
                  u.username, u.role
         ORDER BY s.updated_at DESC
    """)

    audit = db.query(conn, """
        SELECT a.*, u.username
          FROM repo_audit_log a
          LEFT JOIN repo_users u ON u.user_id = a.user_id
         ORDER BY a.performed_at DESC LIMIT 20
    """)

    return templates.TemplateResponse(request, "zeus_dashboard.html", {
        "user": user, "staging": staging, "audit": audit, "stats": stats,
    })


# =========================================================================
# PAGE ROUTES
# =========================================================================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    repos = repo.list_repos(conn, user["user_id"])
    return templates.TemplateResponse(request, "index.html", {
        "user": user, "repos": repos,
    })


@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request, conn=Depends(get_db)):
    """Public repo listing — no login required."""
    user = get_current_user(request, conn)
    repos = db.query(conn, """
        SELECT * FROM repo_repositories
         WHERE visibility = 'public'
         ORDER BY updated_at DESC
    """)
    return templates.TemplateResponse(request, "index.html", {
        "user": user, "repos": repos,
    })


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/new", response_class=HTMLResponse)
def new_repo_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can create repositories.")
    return templates.TemplateResponse(request, "new_repo.html", {"user": user})


@app.post("/api/repos")
def create_repo_api(request: Request,
                    name: str = Form(...),
                    description: str = Form(""),
                    visibility: str = Form("public"),
                    conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can create repositories.")

    name = name.strip()
    if not name:
        raise HTTPException(400, "Repository name is required.")

    import re
    if not re.match(r'^[a-zA-Z0-9_\-]+$', name):
        raise HTTPException(400, "Name can only contain letters, numbers, hyphens, and underscores.")

    if visibility not in ("public", "private", "internal"):
        raise HTTPException(400, "Invalid visibility value.")

    # Check for duplicate name
    existing = repo.get_repo(conn, name)
    if existing:
        raise HTTPException(409, f"A repository named '{name}' already exists.")

    try:
        result = repo.create_repo(
            conn, name, user["user_id"],
            visibility=visibility,
            description=description or None
        )
        return result
    except Exception as e:
        raise HTTPException(500, f"Could not create repository: {e}")


# =========================================================================
# FILE TREE HELPER
# =========================================================================

def _load_file_tree(conn, repo_id: int, branch: str) -> list[dict]:
    """
    Load the file list for a branch from its latest commit's changeset records.
    Returns a list of dicts: {path, size, change_type}

    We reconstruct the current tree by replaying all changesets in rev order:
    adds and modifies build the set, deletes remove from it.
    This is the correct approach until we have a proper tree-object walker.
    """
    ref_name = f"refs/heads/{branch}"

    # Get the current commit hash for this branch
    ref_row = db.query_one(conn,
        "SELECT commit_hash FROM repo_refs WHERE repo_id = %s AND ref_name = %s",
        (repo_id, ref_name))

    if not ref_row or not ref_row["commit_hash"]:
        return []

    # Replay all changesets in order to build current file set
    rows = db.query(conn, """
        SELECT cs.path, cs.change_type, cs.blob_after,
               c.rev
          FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
         WHERE c.repo_id = %s
         ORDER BY c.rev ASC, cs.path ASC
    """, (repo_id,))

    # Build current file set: path -> blob_hash
    tree = {}
    for row in rows:
        if row["change_type"] in ("add", "modify"):
            tree[row["path"]] = row["blob_after"]
        elif row["change_type"] == "delete":
            tree.pop(row["path"], None)
        elif row["change_type"] == "rename" and row.get("old_path"):
            tree.pop(row.get("old_path"), None)
            tree[row["path"]] = row["blob_after"]

    # Build display list sorted by path
    files = []
    for path, blob_hash in sorted(tree.items()):
        committed_at = db.query_scalar(conn, """
            SELECT committed_at FROM repo_file_revisions
             WHERE repo_id = %s AND path = %s AND change_type != 'delete'
             ORDER BY committed_at DESC LIMIT 1
        """, (repo_id, path))

        files.append({
            "path": path,
            "type": "file",
            "size": _format_size(blob_hash, conn, repo_id),
            "committed_at": committed_at.strftime('%Y-%m-%d %H:%M') if committed_at else None,
        })

    return files


def _format_size(blob_hash: str, conn, repo_id: int) -> str:
    """Get human-readable size from repo_objects if available."""
    if not blob_hash:
        return "—"
    row = db.query_one(conn,
        "SELECT size_bytes FROM repo_objects WHERE object_hash = %s AND repo_id = %s",
        (blob_hash, repo_id))
    if not row:
        return "—"
    size = row["size_bytes"]
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size // 1024} KB"
    else:
        return f"{size // (1024 * 1024)} MB"


# =========================================================================
# REPO BROWSER PAGES
# =========================================================================
@app.get("/repo/{name}", response_class=HTMLResponse)
def repo_page(name: str, request: Request, branch: str = None,
              conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404, "Repository not found")

    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403, "Access denied")

    fork_source = None
    fork_row = db.query_one(conn, """
        SELECT details->>'source' as source
          FROM repo_audit_log
         WHERE repo_id = %s AND action = 'repo_fork'
         LIMIT 1
    """, (r["repo_id"],))
    if fork_row and fork_row["source"]:
        fork_source = fork_row["source"]

    branches = repo.get_branches(conn, r["repo_id"])
    current_branch = branch or r.get("default_branch", "main")
    files = _load_file_tree(conn, r["repo_id"], current_branch)

    return templates.TemplateResponse(request, "repo_browser.html", {
        "user": user, "repo": r,
        "branches": branches, "current_branch": current_branch,
        "tab": "files", "files": files,
        "fork_source": fork_source,
    })


@app.get("/repo/{name}/commits", response_class=HTMLResponse)
def repo_commits_page(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    branches = repo.get_branches(conn, r["repo_id"])
    commits = repo.get_log(conn, r["repo_id"], limit=50)

    return templates.TemplateResponse(request, "repo_browser.html", {
        "user": user, "repo": r,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "tab": "commits", "commits": commits,
    })


@app.get("/repo/{name}/mana", response_class=HTMLResponse)
def repo_mana_page(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    branches = repo.get_branches(conn, r["repo_id"])

    messages = db.query(conn, """
        SELECT m.*, u.username FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.repo_id = %s
         ORDER BY m.created_at DESC LIMIT 100
    """, (r["repo_id"],))

    return templates.TemplateResponse(request, "repo_browser.html", {
        "user": user, "repo": r,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "tab": "mana", "messages": messages,
    })


@app.post("/api/repos/{name}/mana")
def send_mana(name: str, request: Request, content: str = Form(...),
              context_type: str = Form("general"), context_id: str = Form(""),
              conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(403)

    db.execute(conn, """
        INSERT INTO repo_messages
            (repo_id, channel, username, sender_id, content, context_type, context_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (r["repo_id"], f"repo-{name}", user["username"], user["user_id"],
          content, context_type, context_id or None))

    # Broadcast to WebSocket subscribers
    import asyncio
    message = {
        "username": user["username"],
        "content": content,
        "context_type": context_type,
        "context_id": context_id or "",
        "created_at": str(__import__("datetime").datetime.now().strftime("%b %d %H:%M")),
    }
    asyncio.create_task(mana_manager.broadcast(f"repo-{name}", message))

    return {"status": "sent"}


@app.post("/api/repos/{name}/upload")
async def upload_files(name: str, request: Request,
                       message: str = Form(...),
                       files: list[UploadFile] = File(...),
                       conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401, "Authentication required")

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404, "Repository not found")

    if not repo.check_visibility(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(403, "Access denied")

    file_data = []
    for f in files:
        content = await f.read()
        if not content:
            continue
        # Use the filename field which contains the relative path
        # e.g. "src/main.py" not just "main.py"
        filepath = f.filename.replace("\\", "/").lstrip("/")
        if not filepath:
            continue
        # Skip ignored patterns
        ignored = ["__pycache__", ".pyc", ".git", "node_modules",
                   ".venv", ".DS_Store", ".olympusrepo"]
        if any(p in filepath for p in ignored):
            continue
        file_data.append((filepath, content))

    if not file_data:
        raise HTTPException(400, "No valid files to commit.")

    # Determine objects directory (adjust according to your config setup)
    objects_dir = os.environ.get(
    "OLYMPUSREPO_OBJECTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "objects")
)

    return repo.commit_files(conn, r["repo_id"], user["user_id"], message, file_data, objects_dir)


@app.websocket("/ws/mana/{channel}")
async def mana_websocket(websocket: WebSocket, channel: str,
                         repo_name: str = None):
    await mana_manager.connect(channel, websocket)
    try:
        while True:
            # Keep alive, client sends pings
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        mana_manager.disconnect(channel, websocket)

@app.get("/repo/{name}/staging", response_class=HTMLResponse)
def repo_staging_page(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    branches = repo.get_branches(conn, r["repo_id"])

    staging_realms = db.query(conn, """
        SELECT s.*, u.username, u.role, COUNT(sc.change_id) as change_count
          FROM repo_staging s
          JOIN repo_users u ON u.user_id = s.user_id
          LEFT JOIN repo_staging_changes sc ON sc.staging_id = s.staging_id
         WHERE s.repo_id = %s AND s.status = 'active'
         GROUP BY s.staging_id, u.username, u.role
         ORDER BY s.updated_at DESC
    """, (r["repo_id"],))

    return templates.TemplateResponse(request, "repo_browser.html", {
        "user": user, "repo": r,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "tab": "staging", "staging_realms": staging_realms,
    })


# =========================================================================
# USER MANAGEMENT (Zeus only)
# =========================================================================

@app.get("/zeus/users", response_class=HTMLResponse)
def users_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can manage users.")

    users = db.query(conn, """
        SELECT user_id, username, full_name, email, role,
               is_active, created_at, last_login
          FROM repo_users
         ORDER BY created_at DESC
    """)

    return templates.TemplateResponse(request, "users.html", {
        "user": user, "users": users,
    })


@app.post("/api/admin/users")
def create_user_api(request: Request,
                    username: str = Form(...),
                    password: str = Form(...),
                    full_name: str = Form(""),
                    email: str = Form(""),
                    role: str = Form("mortal"),
                    conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can create users.")

    valid_roles = ("zeus", "olympian", "titan", "mortal", "prometheus", "hermes")
    if role not in valid_roles:
        raise HTTPException(400, f"Invalid role.")

    try:
        user_id = db.create_user(
            conn, username, password, role,
            full_name=full_name or None,
            email=email or None
        )
        db.audit_log(conn, "user_create", user_id=user["user_id"],
                     target_type="user", target_id=username,
                     details={"role": role}, commit=False)
        conn.commit()
        return {"user_id": user_id, "username": username, "role": role}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        conn.rollback()
        raise HTTPException(409, "Username or email already taken.")


@app.patch("/api/admin/users/{user_id}")
def update_user_api(user_id: int, request: Request,
                    role: str = Form(None),
                    is_active: str = Form(None),
                    conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can update users.")

    if user_id == user["user_id"]:
        raise HTTPException(400, "You cannot modify your own account from this panel.")

    target = db.get_user(conn, user_id)
    if not target:
        raise HTTPException(404, "User not found.")

    updates = []
    params = []

    if role is not None:
        valid_roles = ("zeus", "olympian", "titan", "mortal", "prometheus", "hermes")
        if role not in valid_roles:
            raise HTTPException(400, "Invalid role.")
        updates.append("role = %s")
        params.append(role)

    if is_active is not None:
        active_bool = is_active.lower() in ("true", "1", "yes")
        updates.append("is_active = %s")
        params.append(active_bool)

    if not updates:
        raise HTTPException(400, "Nothing to update.")

    params.append(user_id)
    db.execute(conn, f"UPDATE repo_users SET {', '.join(updates)} WHERE user_id = %s",
               params, commit=False)

    db.audit_log(conn, "user_update", user_id=user["user_id"],
                 target_type="user", target_id=str(user_id),
                 details={"role": role, "is_active": is_active}, commit=False)
    conn.commit()

    return {"status": "updated", "user_id": user_id}


# =========================================================================
# SERVER CONFIG (Zeus only)
# =========================================================================
@app.get("/zeus/config", response_class=HTMLResponse)
def config_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can access server config.")
    config = db.query(conn, "SELECT key, value, updated_at FROM repo_server_config ORDER BY key")
    return templates.TemplateResponse(request, "config.html", {"user": user, "config": config})

@app.post("/api/admin/config")
def update_config(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can update config.")
    import json
    body = {}
    # read raw JSON body
    # FastAPI note: use async or read via request.body() - handle in route below
    raise HTTPException(500, "Use the form endpoint instead.")

@app.post("/api/admin/config/set")
def set_config_value(request: Request,
                     key: str = Form(...),
                     value: str = Form(...),
                     conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can update config.")
    allowed_keys = ("registration_policy", "default_repo_visibility",
                    "instance_name", "instance_url", "max_pack_size_mb")
    if key not in allowed_keys:
        raise HTTPException(400, f"Unknown config key: {key}")
    db.execute(conn,
        "UPDATE repo_server_config SET value = %s, updated_at = NOW(), updated_by = %s WHERE key = %s",
        (value, user["user_id"], key), commit=False)
    db.audit_log(conn, "config_change", user_id=user["user_id"],
                 target_type="config", target_id=key,
                 details={"value": value}, commit=False)
    conn.commit()
    return {"status": "updated", "key": key, "value": value}


# =========================================================================
# REPO SETTINGS (Zeus only)
# =========================================================================
@app.get("/repo/{name}/settings", response_class=HTMLResponse)
def repo_settings_page(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can edit repository settings.")
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "repo_settings.html", {"user": user, "repo": r})

@app.post("/api/repos/{name}/settings")
def update_repo_settings(name: str, request: Request,
                         description: str = Form(""),
                         visibility: str = Form(...),
                         conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403, "Only Zeus can edit repository settings.")
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if visibility not in ("public", "private", "internal"):
        raise HTTPException(400, "Invalid visibility.")
    db.execute(conn, """
        UPDATE repo_repositories
           SET description = %s, visibility = %s, updated_at = NOW()
         WHERE repo_id = %s
    """, (description or None, visibility, r["repo_id"]), commit=False)
    db.audit_log(conn, "repo_update", user_id=user["user_id"],
                 repo_id=r["repo_id"], target_type="repo", target_id=name,
                 details={"visibility": visibility}, commit=False)
    conn.commit()
    return JSONResponse({"status": "updated"})


# =========================================================================
@app.get("/repo/{name}/prune", response_class=HTMLResponse)
def prune_page(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    stats = {
        "total_file_revs": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_file_revisions WHERE repo_id = %s",
            (r["repo_id"],)) or 0,
        "unique_files": db.query_scalar(conn,
            "SELECT COUNT(DISTINCT path) FROM repo_file_revisions WHERE repo_id = %s",
            (r["repo_id"],)) or 0,
        "total_commits": db.query_scalar(conn,
            "SELECT COUNT(*) FROM repo_commits WHERE repo_id = %s",
            (r["repo_id"],)) or 0,
        "archive_log": db.query(conn, """
            SELECT a.*, u.username FROM repo_archive_log a
              LEFT JOIN repo_users u ON u.user_id = a.pruned_by
             WHERE a.repo_id = %s ORDER BY a.pruned_at DESC LIMIT 10
        """, (r["repo_id"],))
    }

    return templates.TemplateResponse(request, "repo_prune.html", {
        "user": user, "repo": r, "stats": stats,
    })


@app.post("/api/repos/{name}/prune")
def prune_repo(name: str, request: Request,
               strategy: str = Form(...),
               keep_n: int = Form(10),
               older_than_days: int = Form(90),
               conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    pruned = 0
    try:
        if strategy == "keep_last_n":
            # Keep only the last N revisions per file, delete the rest
            result = db.query(conn, """
                DELETE FROM repo_file_revisions
                 WHERE frev_id IN (
                     SELECT frev_id FROM (
                         SELECT frev_id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY repo_id, path
                                    ORDER BY committed_at DESC
                                ) as rn
                           FROM repo_file_revisions
                          WHERE repo_id = %s
                     ) ranked WHERE rn > %s
                 )
                 RETURNING frev_id
            """, (r["repo_id"], keep_n))
            pruned = len(result) if result else 0

        elif strategy == "older_than_days":
            # Delete file revisions older than N days
            # But always keep the most recent rev of each file
            result = db.query(conn, """
                DELETE FROM repo_file_revisions
                 WHERE frev_id IN (
                     SELECT fr.frev_id FROM repo_file_revisions fr
                      WHERE fr.repo_id = %s
                        AND fr.committed_at < NOW() - (%s || ' days')::INTERVAL
                        AND fr.committed_at < (
                            SELECT MAX(fr2.committed_at)
                              FROM repo_file_revisions fr2
                             WHERE fr2.repo_id = fr.repo_id
                               AND fr2.path = fr.path
                        )
                 )
                 RETURNING frev_id
            """, (r["repo_id"], older_than_days))
            pruned = len(result) if result else 0

        conn.commit()

        # Log the prune
        db.execute(conn, """
            INSERT INTO repo_archive_log
                (repo_id, pruned_by, strategy, revs_pruned, notes)
            VALUES (%s, %s, %s, %s, %s)
        """, (r["repo_id"], user["user_id"], strategy, pruned,
              f"keep_n={keep_n}" if strategy == "keep_last_n"
              else f"older_than={older_than_days}d"))
        conn.commit()

        return {"status": "pruned", "revs_pruned": pruned}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# =========================================================================
@app.get("/repo/{name}/file/{path:path}", response_class=HTMLResponse)
def file_history_page(name: str, path: str, request: Request,
                      conn=Depends(get_db)):
    """Show full revision history for a specific file."""
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    revisions = db.query(conn, """
        SELECT fr.blob_hash, fr.global_rev, fr.change_type,
               fr.committed_at, fr.author_name, fr.message,
               c.commit_hash
          FROM repo_file_revisions fr
          JOIN repo_commits c ON c.commit_hash = fr.commit_hash
         WHERE fr.repo_id = %s AND fr.path = %s
         ORDER BY fr.committed_at DESC
    """, (r["repo_id"], path))

    if not revisions:
        raise HTTPException(404, f"No revision history for {path}")

    return templates.TemplateResponse(request, "file_history.html", {
        "user": user, "repo": r, "path": path,
        "revisions": revisions,
        "current_rev": revisions[0]["committed_at"] if revisions else None,
    })


@app.get("/repo/{name}/blob/{branch}/{path:path}", response_class=HTMLResponse)
def blob_page(name: str, branch: str, path: str, request: Request,
              at: str = None, conn=Depends(get_db)):
    """
    View file contents. Optional ?at=timestamp shows a specific file revision.
    Without at, shows current version on branch.
    """
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    if at:
        # Load version at specific timestamp
        row = db.query_one(conn, """
            SELECT fr.blob_hash, fr.committed_at, fr.global_rev,
                   fr.change_type, fr.author_name, fr.message,
                   c.commit_hash
              FROM repo_file_revisions fr
              JOIN repo_commits c ON c.commit_hash = fr.commit_hash
             WHERE fr.repo_id = %s AND fr.path = %s
               AND fr.committed_at <= %s::timestamptz
               AND fr.change_type != 'delete'
             ORDER BY fr.committed_at DESC LIMIT 1
        """, (r["repo_id"], path, at))
        if not row:
            raise HTTPException(404, f"{path} not found at {at}")
        blob_hash = row["blob_hash"]
        version_label = row["committed_at"].strftime("%Y-%m-%d %H:%M")
        is_historical = True
    else:
        # Current version
        ref_name = f"refs/heads/{branch}"
        row = db.query_one(conn, """
            SELECT cs.blob_after, fr.committed_at
              FROM repo_changesets cs
              JOIN repo_commits c ON c.commit_hash = cs.commit_hash
              JOIN repo_refs rf ON rf.commit_hash = c.commit_hash
              LEFT JOIN repo_file_revisions fr
                ON fr.commit_hash = cs.commit_hash AND fr.path = cs.path
             WHERE c.repo_id = %s AND rf.ref_name = %s
               AND cs.path = %s AND cs.change_type != 'delete'
             ORDER BY c.rev DESC LIMIT 1
        """, (r["repo_id"], ref_name, path))
        if not row:
            raise HTTPException(404, "File not found.")
        blob_hash = row["blob_after"]
        version_label = row["committed_at"].strftime("%Y-%m-%d %H:%M") if row["committed_at"] else "current"
        is_historical = False

    # Get full history for prev/next navigation
    history = db.query(conn, """
        SELECT committed_at, global_rev, change_type, author_name, message
          FROM repo_file_revisions
         WHERE repo_id = %s AND path = %s
         ORDER BY committed_at DESC
    """, (r["repo_id"], path))

    objects_dir = os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    from ..core import objects as obj_store
    content_bytes = obj_store.retrieve_blob(blob_hash, objects_dir)
    if content_bytes is None:
        raise HTTPException(404, "Blob not found in object store.")

    # Detect if binary
    try:
        content = content_bytes.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        content = None
        is_binary = True

    branches = repo.get_branches(conn, r["repo_id"])
    return templates.TemplateResponse(request, "blob.html", {
        "user": user, "repo": r, "branch": branch, "path": path,
        "content": content, "is_binary": is_binary,
        "branches": branches, "current_branch": branch,
        "version_label": version_label,
        "is_historical": is_historical,
        "history": history,
        "viewing_at": at,
    })


@app.get("/repo/{name}/commit/{commit_hash}", response_class=HTMLResponse)
def commit_page(name: str, commit_hash: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"], user["user_id"] if user else None):
        raise HTTPException(403)

    commit_row = db.query_one(conn, """
        SELECT * FROM repo_commits WHERE commit_hash = %s AND repo_id = %s
    """, (commit_hash, r["repo_id"]))
    if not commit_row:
        raise HTTPException(404, "Commit not found.")

    changesets = db.query(conn, """
        SELECT * FROM repo_changesets WHERE commit_hash = %s ORDER BY path
    """, (commit_hash,))

    branches = repo.get_branches(conn, r["repo_id"])
    return templates.TemplateResponse(request, "commit.html", {
        "user": user, "repo": r, "commit": commit_row,
        "changesets": changesets, "branches": branches,
        "current_branch": r.get("default_branch", "main"),
    })


# =========================================================================
# PASSWORD RESET & FORKING
# =========================================================================
@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    return templates.TemplateResponse(request, "reset_password.html",
                                      {"token": token})

@app.post("/api/auth/request-reset")
def request_password_reset(email: str = Form(...), conn=Depends(get_db)):
    # Find user by email
    user = db.query_one(conn,
        "SELECT * FROM repo_users WHERE email = %s AND is_active = TRUE",
        (email,))
    if not user:
        # Don't reveal whether email exists
        return {"status": "ok", "message": "If that email exists, a reset token has been generated."}

    # Generate token
    import secrets
    token = secrets.token_hex(32)
    db.execute(conn, """
        INSERT INTO repo_password_resets (user_id, token, expires_at)
        VALUES (%s, %s, NOW() + INTERVAL '1 hour')
    """, (user["user_id"], token), commit=False)
    db.audit_log(conn, "password_reset_request", user_id=user["user_id"],
                 target_type="user", target_id=user["username"],
                 commit=False)
    conn.commit()

    # In production send email. For now return token directly
    # so admin can relay it. Log it to console.
    print(f"PASSWORD RESET TOKEN for {user['username']}: /reset-password?token={token}")
    return {
        "status": "ok",
        "message": "Reset token generated. Check server console for the link.",
        "debug_token": token  # Remove in production
    }

@app.post("/api/auth/reset-password")
def do_password_reset(token: str = Form(...),
                      new_password: str = Form(...),
                      conn=Depends(get_db)):
    if len(new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    row = db.query_one(conn, """
        SELECT r.*, u.username FROM repo_password_resets r
          JOIN repo_users u ON u.user_id = r.user_id
         WHERE r.token = %s AND r.used = FALSE AND r.expires_at > NOW()
    """, (token,))
    if not row:
        raise HTTPException(400, "Invalid or expired reset token.")

    try:
        # Update password using pgcrypto
        db.execute(conn, """
            UPDATE repo_users
               SET password_hash = crypt(%s, gen_salt('bf', 10))
             WHERE user_id = %s
        """, (new_password, row["user_id"]), commit=False)

        # Mark token used
        db.execute(conn,
            "UPDATE repo_password_resets SET used = TRUE WHERE reset_id = %s",
            (row["reset_id"],), commit=False)

        db.audit_log(conn, "password_reset", user_id=row["user_id"],
                     target_type="user", target_id=row["username"],
                     commit=False)
        conn.commit()
        return {"status": "ok", "message": "Password updated. You can now log in."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

@app.post("/api/repos/{name}/fork")
def fork_repo(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)

    source = repo.get_repo(conn, name)
    if not source:
        raise HTTPException(404)
    if not repo.check_visibility(conn, source["repo_id"],
                                 user["user_id"]):
        raise HTTPException(403)

    # Build fork name: username-reponame, handle collision
    fork_name = f"{user['username']}-{name}"
    existing = repo.get_repo(conn, fork_name)
    if existing:
        fork_name = f"{fork_name}-{int(__import__('time').time())}"

    try:
        # Create the fork repo record
        fork = repo.create_repo(
            conn, fork_name, user["user_id"],
            visibility="private",
            description=f"Fork of {name}"
        )

        # Copy all refs (branches)
        refs = repo.get_branches(conn, source["repo_id"])
        for ref in refs:
            db.execute(conn, """
                INSERT INTO repo_refs (repo_id, ref_name, commit_hash, updated_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repo_id, ref_name)
                DO UPDATE SET commit_hash = EXCLUDED.commit_hash
            """, (fork["repo_id"], ref["ref_name"],
                  ref["commit_hash"], user["user_id"]), commit=False)

        # Copy commit history reference (commits are shared, not duplicated)
        # Copy changesets are referenced via commits — no duplication needed
        # since content-addressable storage means blobs are shared

        db.audit_log(conn, "repo_fork", user_id=user["user_id"],
                     repo_id=fork["repo_id"], target_type="repo",
                     target_id=name,
                     details={"source": name, "fork": fork_name},
                     commit=False)
        conn.commit()
        return {"status": "forked", "name": fork_name, "repo_id": fork["repo_id"]}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Fork failed: {e}")


@app.get("/zeus/audit", response_class=HTMLResponse)
def audit_log_page(request: Request,
                   action: str = "",
                   username: str = "",
                   repo_name: str = "",
                   limit: int = 100,
                   conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)

    filters = ["1=1"]
    params = []

    if action:
        filters.append("a.action ILIKE %s")
        params.append(f"%{action}%")
    if username:
        filters.append("u.username ILIKE %s")
        params.append(f"%{username}%")
    if repo_name:
        filters.append("r.name ILIKE %s")
        params.append(f"%{repo_name}%")

    params.append(limit)
    where = " AND ".join(filters)

    logs = db.query(conn, f"""
        SELECT a.log_id, a.action, a.target_type, a.target_id,
               a.details, a.ip_address, a.performed_at,
               u.username, r.name as repo_name
          FROM repo_audit_log a
          LEFT JOIN repo_users u ON u.user_id = a.user_id
          LEFT JOIN repo_repositories r ON r.repo_id = a.repo_id
         WHERE {where}
         ORDER BY a.performed_at DESC
         LIMIT %s
    """, params)

    all_actions = db.query(conn,
        "SELECT DISTINCT action FROM repo_audit_log ORDER BY action")
    all_repos = db.query(conn,
        "SELECT name FROM repo_repositories ORDER BY name")

    return templates.TemplateResponse(request, "audit_log.html", {
        "user": user, "logs": logs,
        "filter_action": action,
        "filter_username": username,
        "filter_repo": repo_name,
        "all_actions": [r["action"] for r in all_actions],
        "all_repos": [r["name"] for r in all_repos],
        "limit": limit,
    })


# =========================================================================
# DIRECT MESSAGING (Inbox)
# =========================================================================

@app.get("/messages", response_class=HTMLResponse)
def inbox_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Get all DMs for this user
    messages = db.query(conn, """
        SELECT m.*, u.username as sender_name,
               r.username as recipient_name,
               EXISTS(
                   SELECT 1 FROM repo_message_reads mr
                    WHERE mr.message_id = m.message_id
                      AND mr.user_id = %s
               ) as is_read
          FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
          LEFT JOIN repo_users r ON r.user_id = m.recipient_id
         WHERE m.is_private = TRUE
           AND (m.sender_id = %s OR m.recipient_id = %s)
           AND m.parent_id IS NULL
         ORDER BY m.created_at DESC
         LIMIT 50
    """, (user["user_id"], user["user_id"], user["user_id"]))

    # Get list of users to message
    all_users = db.query(conn, """
        SELECT user_id, username, role FROM repo_users
         WHERE is_active = TRUE AND user_id != %s
         ORDER BY username
    """, (user["user_id"],))

    unread = sum(1 for m in messages
                 if not m["is_read"] and m["sender_id"] != user["user_id"])

    return templates.TemplateResponse(request, "inbox.html", {
        "user": user, "messages": messages,
        "all_users": all_users, "unread": unread,
    })


@app.get("/messages/{message_id}", response_class=HTMLResponse)
def message_thread_page(message_id: int, request: Request,
                        conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Get the root message
    root = db.query_one(conn, """
        SELECT m.*, u.username as sender_name,
               r.username as recipient_name
          FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
          LEFT JOIN repo_users r ON r.user_id = m.recipient_id
         WHERE m.message_id = %s
           AND m.is_private = TRUE
           AND (m.sender_id = %s OR m.recipient_id = %s)
    """, (message_id, user["user_id"], user["user_id"]))

    if not root:
        raise HTTPException(404)

    # Get thread replies
    replies = db.query(conn, """
        SELECT m.*, u.username as sender_name
          FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.thread_id = %s OR m.parent_id = %s
         ORDER BY m.created_at ASC
    """, (message_id, message_id))

    # Mark as read
    db.execute(conn, """
        INSERT INTO repo_message_reads (user_id, message_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
    """, (user["user_id"], message_id))

    return templates.TemplateResponse(request, "message_thread.html", {
        "user": user, "root": root, "replies": replies,
    })


@app.post("/api/messages")
def send_direct_message(request: Request,
                        recipient_id: int = Form(...),
                        content: str = Form(...),
                        subject: str = Form(""),
                        parent_id: int = Form(None),
                        thread_id: int = Form(None),
                        conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)

    if not content.strip():
        raise HTTPException(400, "Message cannot be empty.")

    recipient = db.get_user(conn, recipient_id)
    if not recipient:
        raise HTTPException(404, "Recipient not found.")

    try:
        msg_id = db.query_scalar(conn, """
            INSERT INTO repo_messages
                (channel, username, sender_id, recipient_id,
                 content, context_type, is_private,
                 parent_id, thread_id)
            VALUES ('dm', %s, %s, %s, %s, 'direct', TRUE, %s, %s)
            RETURNING message_id
        """, (user["username"], user["user_id"], recipient_id,
              content.strip(), parent_id, thread_id or parent_id))

        # Update reply count on parent
        if parent_id:
            db.execute(conn, """
                UPDATE repo_messages
                   SET reply_count = reply_count + 1
                 WHERE message_id = %s
            """, (parent_id,), commit=False)

        # Create notification for recipient
        db.create_notification(
            conn,
            user_id=recipient_id,
            notif_type="direct_message",
            message=f"{user['username']} sent you a message.",
            link=f"/messages/{thread_id or msg_id}",
            commit=False
        )

        conn.commit()
        return {"status": "sent", "message_id": msg_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@app.get("/api/messages/unread-count")
def unread_count(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        return {"unread": 0}
    count = db.query_scalar(conn, """
        SELECT COUNT(*) FROM repo_messages m
         WHERE m.recipient_id = %s
           AND m.is_private = TRUE
           AND NOT EXISTS (
               SELECT 1 FROM repo_message_reads mr
                WHERE mr.message_id = m.message_id
                  AND mr.user_id = %s
           )
    """, (user["user_id"], user["user_id"])) or 0
    return {"unread": count}


# =========================================================================
# INLINE CODE COMMENTS
# =========================================================================

@app.get("/api/repos/{name}/comments/{path:path}")
def get_file_comments(name: str, path: str, request: Request,
                      conn=Depends(get_db)):
    """Get all inline comments for a file."""
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    comments = db.query(conn, """
        SELECT m.message_id, m.content, m.context_id,
               m.created_at, m.reply_count, m.parent_id,
               u.username, u.role
          FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.repo_id = %s
           AND m.context_type = 'file'
           AND m.context_id LIKE %s
           AND m.is_private = FALSE
           AND m.parent_id IS NULL
         ORDER BY m.created_at ASC
    """, (r["repo_id"], f"{path}:%"))

    # Parse line numbers from context_id "filepath:linenum"
    result = []
    for c in comments:
        parts = c["context_id"].split(":")
        line_num = int(parts[-1]) if len(parts) > 1 and parts[-1].isdigit() else 0
        result.append({**dict(c), "line_num": line_num})

    return result


@app.post("/api/repos/{name}/comments")
def post_inline_comment(name: str, request: Request,
                        path: str = Form(...),
                        line_num: int = Form(...),
                        content: str = Form(...),
                        parent_id: int = Form(None),
                        conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(403)

    context_id = f"{path}:{line_num}"

    try:
        msg_id = db.query_scalar(conn, """
            INSERT INTO repo_messages
                (repo_id, channel, username, sender_id, content,
                 context_type, context_id, is_private,
                 parent_id, thread_id)
            VALUES (%s, %s, %s, %s, %s, 'file', %s, FALSE, %s, %s)
            RETURNING message_id
        """, (r["repo_id"], f"repo-{name}", user["username"],
              user["user_id"], content.strip(),
              context_id, parent_id, parent_id))

        if parent_id:
            db.execute(conn, """
                UPDATE repo_messages SET reply_count = reply_count + 1
                 WHERE message_id = %s
            """, (parent_id,), commit=False)

        conn.commit()
        return {"status": "posted", "message_id": msg_id, "line_num": line_num}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@app.post("/api/repos/{name}/commit/{commit_hash}/comments")
def post_commit_comment(name: str, commit_hash: str,
                        request: Request,
                        content: str = Form(...),
                        parent_id: int = Form(None),
                        conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    try:
        msg_id = db.query_scalar(conn, """
            INSERT INTO repo_messages
                (repo_id, channel, username, sender_id, content,
                 context_type, context_id, is_private,
                 parent_id, thread_id)
            VALUES (%s, %s, %s, %s, %s, 'commit', %s, FALSE, %s, %s)
            RETURNING message_id
        """, (r["repo_id"], f"repo-{name}", user["username"],
              user["user_id"], content.strip(),
              commit_hash, parent_id, parent_id))

        if parent_id:
            db.execute(conn, """
                UPDATE repo_messages SET reply_count = reply_count + 1
                 WHERE message_id = %s
            """, (parent_id,), commit=False)

        conn.commit()
        return {"status": "posted", "message_id": msg_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@app.get("/api/repos/{name}/commit/{commit_hash}/comments")
def get_commit_comments(name: str, commit_hash: str,
                        request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    return db.query(conn, """
        SELECT m.message_id, m.content, m.created_at,
               m.reply_count, m.parent_id,
               u.username, u.role
          FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.repo_id = %s
           AND m.context_type = 'commit'
           AND m.context_id = %s
           AND m.is_private = FALSE
           AND m.parent_id IS NULL
         ORDER BY m.created_at ASC
    """, (r["repo_id"], commit_hash))


# =========================================================================
# RUN
# =========================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)