
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License
#
# Run: uvicorn olympusrepo.web.app:app --reload --host 0.0.0.0 --port 8000
#
# Environment variables:
#   OLYMPUSREPO_COOKIE_SECURE=1   set secure cookie flag (for HTTPS production)

import os
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..core import db, repo

app = FastAPI(title="OlympusRepo", version="0.2")

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

COOKIE_SECURE = os.getenv("OLYMPUSREPO_COOKIE_SECURE", "0") == "1"


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
        # Set RLS context for any subsequent queries in this request
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
    response.delete_cookie("session_id")
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


# =========================================================================
# ZEUS DASHBOARD
# =========================================================================
@app.get("/zeus", response_class=HTMLResponse)
def zeus_dashboard(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403, "The Throne is reserved for Zeus and the Olympian council.")

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

    return templates.TemplateResponse("zeus_dashboard.html", {
        "request": request, "user": user, "staging": staging, "audit": audit,
    })


# =========================================================================
# PAGE ROUTES
# =========================================================================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    user_id = user["user_id"] if user else None
    repos = repo.list_repos(conn, user_id)

    return templates.TemplateResponse("index.html", {
        "request": request, "user": user, "repos": repos,
    })


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


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

    branches = repo.get_branches(conn, r["repo_id"])
    current_branch = branch or r.get("default_branch", "main")

    # TODO: load file tree from latest commit tree object
    files = []

    return templates.TemplateResponse("repo_browser.html", {
        "request": request, "user": user, "repo": r,
        "branches": branches, "current_branch": current_branch,
        "tab": "files", "files": files,
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

    return templates.TemplateResponse("repo_browser.html", {
        "request": request, "user": user, "repo": r,
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

    # RLS context is already set by get_current_user if user is logged in.
    # Anonymous users see only public mana (is_private=FALSE).
    messages = db.query(conn, """
        SELECT m.*, u.username FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.repo_id = %s
         ORDER BY m.created_at DESC LIMIT 100
    """, (r["repo_id"],))

    return templates.TemplateResponse("repo_browser.html", {
        "request": request, "user": user, "repo": r,
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

    return {"status": "sent"}


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

    return templates.TemplateResponse("repo_browser.html", {
        "request": request, "user": user, "repo": r,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "tab": "staging", "staging_realms": staging_realms,
    })


# =========================================================================
# RUN
# =========================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
