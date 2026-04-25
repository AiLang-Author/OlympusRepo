# olympusrepo/web/app.py
# FastAPI web server for OlympusRepo
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering (SCSL)
#
# Run: uvicorn olympusrepo.web.app:app --reload --host 0.0.0.0 --port 8000
#
# Environment variables:
#   OLYMPUSREPO_COOKIE_SECURE=1   set secure cookie flag (for HTTPS production)

import os
import subprocess
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Form, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import HTTPException as FastAPIHTTPException
import asyncio
import json as json_mod
import base64
import re

import urllib.request
import urllib.error
import json as json_stdlib
import threading
import time
import httpx
from contextlib import asynccontextmanager

from ..core import db, repo
from ..core import identity as identity_mod
from ..relay_bootstrap import get_relay_list

RELAY_ENABLED    = os.environ.get("OLYMPUSREPO_RELAY_ENABLED", "1") == "1"
RELAY_PORT       = int(os.environ.get("PORT", os.environ.get("OLYMPUSREPO_PORT", 8000)))
HEARTBEAT_INTERVAL = 300  # 5 minutes


# =============================================================================
# In-process rate limiter for auth endpoints.
# Simple IP+route token bucket — no external dependency. Sufficient for
# single-process uvicorn; multi-process or multi-host deployments should
# front this with a real limiter (nginx, traefik, redis-backed slowapi).
# =============================================================================
_rate_lock    = threading.Lock()
_rate_buckets: dict = {}  # (route_key, ip) -> list[unix_timestamp_float]

def _rate_limit(request: Request, route_key: str,
                max_hits: int, window_sec: int) -> None:
    """Raise 429 if the caller has exceeded max_hits for this route
    inside the trailing window_sec seconds. Sets a Retry-After header."""
    ip = request.client.host if request.client else "unknown"
    key = (route_key, ip)
    now = time.time()
    cutoff = now - window_sec
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(key, ()) if t >= cutoff]
        if len(bucket) >= max_hits:
            oldest = bucket[0]
            retry_after = max(1, int(oldest + window_sec - now))
            _rate_buckets[key] = bucket  # keep the pruned list for next try
            raise HTTPException(
                status_code=429,
                detail=f"Too many attempts. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
        _rate_buckets[key] = bucket


def _register_with_relay(relay_url: str, identity: dict,
                         port: int) -> bool:
    """
    Send a signed heartbeat to one relay. Returns True on success.
    Silent on failure — relay is enhancement, not requirement.
    """
    try:
        envelope = identity_mod.make_heartbeat(identity, port=port)
        r = httpx.post(
            relay_url.rstrip("/") + "/relay/register",
            json=envelope,
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def _heartbeat_loop(identity: dict, port: int):
    """
    Background thread — registers with all known relays every 5 minutes.
    Starts immediately on first iteration, then sleeps.
    """
    while True:
        if RELAY_ENABLED:
            relays = get_relay_list()
            ok = 0
            for relay_url in relays:
                if _register_with_relay(relay_url, identity, port):
                    ok += 1
            if ok:
                pass  # registered with at least one relay
        time.sleep(HEARTBEAT_INTERVAL)


@asynccontextmanager
async def lifespan(app):
    # ── Identity ──────────────────────────────────────────────────────────
    ident = identity_mod.load_or_create()
    app.state.identity = ident
    print(f"  Instance: {ident['human_name']} ({ident['instance_id'][:16]}...)")

    # ── Relay heartbeat thread ────────────────────────────────────────────
    if RELAY_ENABLED:
        relays = get_relay_list()
        print(f"  Relay:    enabled — {len(relays)} relay(s) configured")
        t = threading.Thread(
            target=_heartbeat_loop,
            args=(ident, RELAY_PORT),
            daemon=True,
            name="relay-heartbeat",
        )
        t.start()
    else:
        print("  Relay:    disabled (OLYMPUSREPO_RELAY_ENABLED=0)")

    yield
    # shutdown — nothing to clean up (daemon thread dies with process)

app = FastAPI(title="OlympusRepo", version="0.2", lifespan=lifespan)

from fastapi.staticfiles import StaticFiles
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

COOKIE_SECURE = os.getenv("OLYMPUSREPO_COOKIE_SECURE", "0") == "1"


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    conn = db.connect()
    try:
        user = get_current_user(request, conn)
        return templates.TemplateResponse(request, "403.html",
            {"user": user}, status_code=403)
    finally:
        conn.close()

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    conn = db.connect()
    try:
        user = get_current_user(request, conn)
        return templates.TemplateResponse(request, "404.html",
            {"user": user}, status_code=404)
    finally:
        conn.close()

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
    # Rate limit: 5 attempts per IP per 15 minutes. Protects against
    # brute-force password guessing and credential-stuffing.
    _rate_limit(request, "login", max_hits=5, window_sec=900)

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
def signup(request: Request,
           username: str = Form(...), password: str = Form(...),
           email: str = Form(None), full_name: str = Form(None),
           conn=Depends(get_db)):
    # Rate limit: 3 signups per IP per hour. Slows user-enumeration via
    # "username already taken" errors and prevents automated account farms.
    _rate_limit(request, "signup", max_hits=3, window_sec=3600)

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


@app.get("/api/users/search")
def search_users(q: str = "", request: Request = None,
                 conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    if len(q) < 2:
        return []
    return db.query(conn, """
        SELECT user_id, username, role, full_name
          FROM repo_users
         WHERE is_active = TRUE
           AND (username ILIKE %s OR full_name ILIKE %s)
         ORDER BY username LIMIT 10
    """, (f"%{q}%", f"%{q}%"))


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


@app.get("/zeus/repos", response_class=HTMLResponse)
def zeus_repos_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403, "The Throne is reserved for Zeus and the Olympian council.")

    repos = db.query(conn, """
        SELECT r.*, u.username as owner_name
          FROM repo_repositories r
          LEFT JOIN repo_users u ON u.user_id = r.owner_id
         ORDER BY r.name ASC
    """)

    return templates.TemplateResponse(request, "zeus_repos.html", {
        "user": user, "repos": repos,
    })


@app.get("/zeus/staging", response_class=HTMLResponse)
def zeus_staging_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403, "The Throne is reserved for Zeus and the Olympian council.")

    staging = db.query(conn, """
        SELECT s.*, u.username, u.role, r.name as repo_name,
               COUNT(sc.change_id) as change_count
          FROM repo_staging s
          JOIN repo_users u ON u.user_id = s.user_id
          JOIN repo_repositories r ON r.repo_id = s.repo_id
          LEFT JOIN repo_staging_changes sc ON sc.staging_id = s.staging_id
         WHERE s.status = 'active'
         GROUP BY s.staging_id, u.username, u.role, r.name
         ORDER BY s.updated_at DESC
    """)

    return templates.TemplateResponse(request, "zeus_staging.html", {
        "user": user, "staging": staging,
    })


@app.get("/zeus/promotions", response_class=HTMLResponse)
def zeus_promotions_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403, "The Throne is reserved for Zeus and the Olympian council.")

    promotions = db.query(conn, """
        SELECT p.*, u.username as promoted_by_name,
               r.name as repo_name, c.commit_hash, c.message, c.rev
          FROM repo_promotions p
          JOIN repo_users u ON u.user_id = p.promoted_by
          JOIN repo_repositories r ON r.repo_id = p.repo_id
          JOIN repo_commits c ON c.commit_hash = p.commit_hash
         ORDER BY p.promoted_at DESC LIMIT 100
    """)

    return templates.TemplateResponse(request, "zeus_promotions.html", {
        "user": user, "promotions": promotions,
    })


@app.get("/zeus/commits", response_class=HTMLResponse)
def zeus_commits_page(request: Request, today: str = "0", conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403, "The Throne is reserved for Zeus and the Olympian council.")

    is_today = today == "1"
    where_clause = "c.committed_at >= CURRENT_DATE" if is_today else "1 = 1"

    commits = db.query(conn, f"""
        SELECT c.*, r.name as repo_name, u.username
          FROM repo_commits c
          JOIN repo_repositories r ON r.repo_id = c.repo_id
          LEFT JOIN repo_users u ON u.user_id = c.author_id
         WHERE {where_clause}
         ORDER BY c.committed_at DESC LIMIT 200
    """)

    return templates.TemplateResponse(request, "zeus_commits.html", {
        "user": user, "commits": commits, "today": is_today,
    })


@app.get("/zeus/relay", response_class=HTMLResponse)
def zeus_relay(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)

    from ..core import identity as identity_mod
    from ..relay_bootstrap import get_relay_list
    import httpx as _httpx

    ident   = identity_mod.load_or_create()
    env_name = os.environ.get("OLYMPUSREPO_INSTANCE_NAME", "").strip()
    if env_name:
        ident["human_name"] = env_name
    relays  = get_relay_list()
    enabled = os.environ.get("OLYMPUSREPO_RELAY_ENABLED", "1") == "1"

    # Check registration status on each configured relay
    relay_statuses = []
    for url in relays:
        status = {"url": url, "reachable": False, "instances": None}
        try:
            r = _httpx.get(url.rstrip("/") + "/relay/health", timeout=3)
            if r.status_code == 200:
                data = r.json()
                status["reachable"]  = True
                status["instances"]  = data.get("instances", 0)
                status["relay_id"]   = data.get("relay_id", "")
        except Exception:
            pass
        relay_statuses.append(status)

    # Check if this instance is registered on any reachable relay
    registered_on = []
    for s in relay_statuses:
        if not s["reachable"]:
            continue
        try:
            r = _httpx.get(
                s["url"].rstrip("/") + f"/relay/find/{ident['instance_id']}",
                timeout=3)
            if r.status_code == 200:
                registered_on.append(s["url"])
        except Exception:
            pass

    return templates.TemplateResponse(request, "zeus_relay.html", {
        "user":           user,
        "identity":       ident,
        "relay_enabled":  enabled,
        "relay_statuses": relay_statuses,
        "registered_on":  registered_on,
        "relay_urls":     relays,
    })


@app.post("/zeus/relay/save", response_class=HTMLResponse)
async def zeus_relay_save(request: Request, conn=Depends(get_db)):
    """Save relay config to .env and reload env vars."""
    user = get_current_user(request, conn)
    if not user or user["role"] != "zeus":
        raise HTTPException(403)

    form = await request.form()
    enabled       = "1" if form.get("relay_enabled") else "0"
    instance_name = form.get("instance_name", "").strip()
    relay_urls    = form.get("relay_urls", "").strip()

    # Update live env (takes effect immediately without restart)
    os.environ["OLYMPUSREPO_RELAY_ENABLED"]  = enabled
    if instance_name:
        os.environ["OLYMPUSREPO_INSTANCE_NAME"] = instance_name
    if relay_urls:
        os.environ["OLYMPUSREPO_RELAYS"] = relay_urls

    # Patch .env file
    env_path = os.path.join(
        os.path.dirname(__file__), "..", "..", ".env")
    env_path = os.path.normpath(env_path)

    lines = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    def _set(key, val, lines):
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
                lines[i] = f"{key}={val}\n"
                return lines
        lines.append(f"{key}={val}\n")
        return lines

    lines = _set("OLYMPUSREPO_RELAY_ENABLED",  enabled,       lines)
    lines = _set("OLYMPUSREPO_INSTANCE_NAME",  instance_name, lines)
    lines = _set("OLYMPUSREPO_RELAYS",         relay_urls,    lines)

    with open(env_path, "w") as f:
        f.writelines(lines)

    db.audit_log(conn, "relay_config_save", user_id=user["user_id"],
                 details={"enabled": enabled, "relay_urls": relay_urls})

    return RedirectResponse("/zeus/relay?saved=1", status_code=303)


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


# =========================================================================
# GIT IMPORT — bring an existing git repository into OlympusRepo
# =========================================================================
def _validate_git_source(src: str) -> tuple[bool, str]:
    """Return (ok, error_message). Allows HTTPS/HTTP/git:// remote URLs,
    ssh-style 'git@host:path', or an absolute local directory path. Blocks
    leading-dash sources, RFC1918/loopback/link-local hosts (SSRF), and
    protocols outside the allowlist."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    if not src or len(src) > 2000:
        return False, "Git source is empty or too long."
    if src.startswith("-"):
        return False, "Git source must not start with '-'."

    # Local path — require absolute + existing directory.
    if src.startswith("/"):
        if not os.path.isdir(src):
            return False, "Local path is not an existing directory."
        return True, ""

    # ssh-style: git@host:path (no scheme, rely on ssh config for auth).
    if src.startswith("git@"):
        # Very conservative: just ensure a colon-separated shape with no
        # option-like segments. We can't resolve the SSH host without
        # allowing far too much, so leave it to the operator's ssh config.
        if ":" not in src or " " in src:
            return False, "Malformed ssh git URL."
        return True, ""

    # Scheme-based URL: only http(s)/git. No file://, no ext::, no ssh://.
    parsed = urlparse(src)
    if parsed.scheme not in ("http", "https", "git"):
        return False, "Only https://, http://, git://, or git@host:path URLs are allowed."
    if not parsed.hostname:
        return False, "Missing hostname in URL."
    if parsed.username or parsed.password:
        return False, "Credentials in URL are not allowed — use ssh or a deploy key."

    # SSRF guard: resolve hostname and reject any private/loopback/link-local
    # address. Operators who truly need internal mirrors can set
    # OLYMPUSREPO_IMPORT_ALLOW_PRIVATE=1.
    if os.environ.get("OLYMPUSREPO_IMPORT_ALLOW_PRIVATE") != "1":
        try:
            infos = socket.getaddrinfo(parsed.hostname, None)
        except socket.gaierror:
            return False, "Host could not be resolved."
        for family, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if (addr.is_private or addr.is_loopback or
                    addr.is_link_local or addr.is_reserved or
                    addr.is_multicast or addr.is_unspecified):
                return False, f"Host resolves to a non-routable address ({ip})."
    return True, ""


@app.get("/import", response_class=HTMLResponse)
def import_git_page(request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)
    return templates.TemplateResponse(request, "import_git.html", {
        "user": user,
        "error": None,
        "success": None,
    })


@app.post("/import", response_class=HTMLResponse)
async def import_git_submit(
    request: Request,
    git_url: str = Form(...),
    repo_name: str = Form(...),
    branch: str = Form(""),
    conn=Depends(get_db),
):
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)

    git_url   = git_url.strip()
    repo_name = repo_name.strip()
    branch    = branch.strip()

    def _render(error=None, success=None):
        return templates.TemplateResponse(request, "import_git.html", {
            "user":      user,
            "error":     error,
            "success":   success,
            "git_url":   git_url if error else "",
            "repo_name": repo_name if error else "",
        })

    if not re.match(r'^[a-zA-Z0-9_\-]+$', repo_name):
        return _render(
            "Repository name may only contain letters, numbers, "
            "hyphens, and underscores."
        )
    if repo.get_repo(conn, repo_name):
        return _render(
            f"Repository '{repo_name}' already exists. Choose a different name."
        )

    ok, reason = _validate_git_source(git_url)
    if not ok:
        return _render(reason)

    if branch and not re.match(r'^[A-Za-z0-9][A-Za-z0-9/_.\-]{0,199}$', branch):
        return _render("Invalid branch name.")

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )

    from ..core import import_git as ig
    try:
        result = ig.import_git_repo(
            conn=conn,
            git_source=git_url,
            repo_name=repo_name,
            user_id=user["user_id"],
            objects_dir=objects_dir,
            branch=branch or None,
        )
        db.audit_log(conn, "git_import", user_id=user["user_id"],
                     target_type="repo", target_id=repo_name,
                     details={"source": git_url, "branch": branch or ""})
        return _render(success=result)
    except subprocess.TimeoutExpired:
        return _render("Git clone timed out. Try a smaller repo or local path.")
    except subprocess.CalledProcessError as e:
        # Surface git's actual stderr so the user can see 'repo not found',
        # 'auth required', cert issues, etc. — not just the opaque rc.
        detail = (getattr(e, "stderr", "") or "").strip()
        if detail:
            # Trim long diagnostic chatter to one screenful.
            detail = detail.replace("\r", "").strip()
            if len(detail) > 400:
                detail = detail[:400] + "…"
            return _render(f"Git command failed (rc {e.returncode}): {detail}")
        return _render(f"Git command failed with exit code {e.returncode} (no stderr captured — check server console).")
    except ValueError as e:
        return _render(str(e))
    except Exception as e:
        return _render(f"Import failed: {e}")


# =========================================================================
# GIT REMOTES — per-repo git push/pull configuration
# =========================================================================
# Browse, add, remove, and trigger push/pull against the named git
# remotes for a repo. Backed by olympusrepo.core.git_remotes / export_git
# / pull_git. Read access for view; write access (owner / repo_access
# write|admin) required for any mutation including push/pull.

def _get_objects_dir() -> str:
    return os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects"),
    )


def _get_mirrors_dir() -> str:
    return os.environ.get(
        "OLYMPUSREPO_MIRRORS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "mirrors"),
    )


def _can_manage_remotes(conn, user, repo_id: int) -> bool:
    """True if user may add/delete remotes or trigger push/pull. Zeus has
    instance-wide override; otherwise falls back to per-repo write
    access (owner / explicit repo_access write|admin)."""
    if not user:
        return False
    if user.get("role") == "zeus":
        return True
    return repo.check_can_write(conn, repo_id, user["user_id"])


@app.get("/repo/{name}/remotes", response_class=HTMLResponse)
def repo_remotes_page(name: str, request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    from ..core import git_remotes as gr
    remotes = gr.list_remotes(conn, r["repo_id"])
    branches = repo.get_branches(conn, r["repo_id"])
    # Same auth helper used by the mutation endpoints — Zeus has
    # instance-wide override for remote management; everyone else needs
    # owner / write|admin grant on the specific repo.
    can_write = _can_manage_remotes(conn, user, r["repo_id"])

    # Pull recent push + pull log entries per remote so the page can
    # show "what happened last and was it ok" without a second
    # roundtrip. Cheap query — indexed on (repo_id, started_at DESC).
    push_log_by_remote = {}
    pull_log_by_remote = {}
    for rem in remotes:
        push_log_by_remote[rem["remote_id"]] = db.query(conn, """
            SELECT ref_name, from_sha, to_sha,
                   commits_pushed, blobs_pushed, bytes_pushed,
                   status, error_message, started_at, finished_at
              FROM repo_git_push_log
             WHERE repo_id = %s AND remote_id = %s
             ORDER BY started_at DESC
             LIMIT 5
        """, (r["repo_id"], rem["remote_id"]))
        pull_log_by_remote[rem["remote_id"]] = db.query(conn, """
            SELECT ref_name, from_sha, to_sha,
                   commits_fetched,
                   status, error_message, started_at, finished_at
              FROM repo_git_pull_log
             WHERE repo_id = %s AND remote_id = %s
             ORDER BY started_at DESC
             LIMIT 5
        """, (r["repo_id"], rem["remote_id"]))

    return templates.TemplateResponse(request, "git_remotes.html", {
        "user": user,
        "repo": r,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "tab": "remotes",
        "remotes": remotes,
        "can_write": can_write,
        "push_log_by_remote": push_log_by_remote,
        "pull_log_by_remote": pull_log_by_remote,
    })


@app.post("/api/repos/{name}/remotes/{remote_name}/test")
def test_remote_api(name: str, remote_name: str, request: Request,
                    conn=Depends(get_db)):
    """Lightweight reachability check. Runs `git ls-remote --heads <url>`
    and returns the remote's branch list (or the error). Used by the
    'Test Connection' button in the remotes UI so users find creds /
    URL problems before triggering a full push or pull."""
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not _can_manage_remotes(conn, user, r["repo_id"]):
        raise HTTPException(403)

    from ..core import git_remotes as gr
    from ..core import import_git as ig
    remote = gr.get_remote(conn, r["repo_id"], remote_name)
    if not remote:
        raise HTTPException(404, "Remote not found.")
    url = gr.build_authenticated_url(remote)
    try:
        result = subprocess.run(
            [ig.GIT_BIN, *ig.GIT_SAFE_ARGS,
             "ls-remote", "--heads", url],
            capture_output=True, text=True,
            env=ig.GIT_ENV,
            timeout=30,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "error": (result.stderr or "").strip()[:400],
            }
        # ls-remote output: "<sha>\t<refname>" per line
        heads = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                heads.append({"sha": parts[0], "ref": parts[1]})
        return {"ok": True, "heads": heads[:50], "head_count": len(heads)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Connection timed out (30s)."}


@app.post("/api/repos/{name}/remotes")
def add_remote_api(name: str, request: Request,
                   remote_name: str = Form(...),
                   url: str = Form(...),
                   auth_type: str = Form("none"),
                   credential: str = Form(""),
                   conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not _can_manage_remotes(conn, user, r["repo_id"]):
        raise HTTPException(403, "Write access required to manage remotes.")

    from ..core import git_remotes as gr
    try:
        result = gr.add_remote(
            conn, repo_id=r["repo_id"],
            name=remote_name.strip(),
            url=url.strip(),
            user_id=user["user_id"],
            auth_type=auth_type,
            credential=(credential or None) if auth_type != "none" else None,
        )
        db.audit_log(conn, "remote_add", user_id=user["user_id"],
                     repo_id=r["repo_id"], target_type="remote",
                     target_id=remote_name,
                     details={"url": url, "auth_type": auth_type})
        return {"status": "added", "remote_id": result.get("remote_id")}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/repos/{name}/remotes/{remote_name}")
def delete_remote_api(name: str, remote_name: str, request: Request,
                      conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not _can_manage_remotes(conn, user, r["repo_id"]):
        raise HTTPException(403)

    from ..core import git_remotes as gr
    gr.delete_remote(conn, r["repo_id"], remote_name)
    db.audit_log(conn, "remote_delete", user_id=user["user_id"],
                 repo_id=r["repo_id"], target_type="remote",
                 target_id=remote_name)
    return {"status": "deleted"}


@app.post("/api/repos/{name}/remotes/{remote_name}/push")
def push_remote_api(name: str, remote_name: str, request: Request,
                    ref_name: str = Form("refs/heads/main"),
                    force: str = Form(""),
                    conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not _can_manage_remotes(conn, user, r["repo_id"]):
        raise HTTPException(403)

    from ..core import export_git as eg
    try:
        result = eg.push_to_git(
            conn,
            repo_id=r["repo_id"],
            remote_name=remote_name,
            ref_name=ref_name,
            user_id=user["user_id"],
            objects_dir=_get_objects_dir(),
            force=(force == "1" or force.lower() == "true"),
        )
        return {"status": "pushed", "result": result}
    except subprocess.CalledProcessError as e:
        detail = (getattr(e, "stderr", "") or "").strip()[:600]
        raise HTTPException(502, f"git push failed (rc {e.returncode}): {detail}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/repos/{name}/remotes/{remote_name}/pull")
def pull_remote_api(name: str, remote_name: str, request: Request,
                    branch: str = Form("main"),
                    conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not _can_manage_remotes(conn, user, r["repo_id"]):
        raise HTTPException(403)

    from ..core import pull_git as pg
    try:
        result = pg.pull_from_git(
            conn,
            repo_id=r["repo_id"],
            remote_name=remote_name,
            branch=branch,
            user_id=user["user_id"],
            objects_dir=_get_objects_dir(),
            mirrors_root=_get_mirrors_dir(),
        )
        return {"status": "pulled", "result": result}
    except subprocess.CalledProcessError as e:
        detail = (getattr(e, "stderr", "") or "").strip()[:600]
        raise HTTPException(502, f"git pull failed (rc {e.returncode}): {detail}")
    except ValueError as e:
        raise HTTPException(400, str(e))


# =========================================================================
# GIT SMART-HTTP PROTOCOL — public clone/fetch endpoints
# =========================================================================
# Mounts olympusrepo/web/git_protocol.py at the root, exposing:
#   GET  /<repo_name>.git/info/refs
#   POST /<repo_name>.git/git-upload-pack    (clone / fetch)
#   POST /<repo_name>.git/git-receive-pack   (push)
# Auth via session cookie OR PAT bearer token (see olympusrepo/core/pats.py).
# This is what makes `git clone http://host/<repo>.git` work from outside.
from .git_protocol import router as _git_protocol_router
app.include_router(_git_protocol_router)


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
    
    BINARY_EXTENSIONS = {
        '.png','.jpg','.jpeg','.gif','.ico','.svg',
        '.pdf','.zip','.tar','.gz','.whl','.pyc',
        '.pyo','.so','.dll','.exe','.bin'
    }
    for path, blob_hash in sorted(tree.items()):
        try:
            committed_at = db.query_scalar(conn, """
                SELECT committed_at FROM repo_file_revisions
                 WHERE repo_id = %s AND path = %s AND change_type != 'delete'
                 ORDER BY committed_at DESC LIMIT 1
            """, (repo_id, path))
        except Exception:
            committed_at = None

        ext = os.path.splitext(path)[1].lower()
        file_type = "binary" if ext in BINARY_EXTENSIONS else "file"

        files.append({
            "path": path,
            "type": file_type,
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

    # Only root messages — replies are visible inside the thread view
    # at /repo/{name}/mana/{message_id}.
    messages = db.query(conn, """
        SELECT m.*, u.username FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.repo_id = %s AND m.parent_id IS NULL
         ORDER BY m.created_at DESC LIMIT 100
    """, (r["repo_id"],))

    return templates.TemplateResponse(request, "repo_browser.html", {
        "user": user, "repo": r,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "tab": "mana", "messages": messages,
    })


@app.post("/api/repos/{name}/mana")
async def send_mana(name: str, request: Request, content: str = Form(...),
                    context_type: str = Form("general"),
                    context_id: str = Form(""),
                    conn=Depends(get_db)):
    # Async-def is mandatory: this handler awaits the WebSocket broadcast.
    # Earlier sync version called asyncio.create_task() from the threadpool
    # worker thread, which has no running event loop — that raised
    # RuntimeError AFTER the row was committed, producing the
    # "message saved but UI says 'not sent'" symptom users were hitting.
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(403)

    msg_id = db.query_scalar(conn, """
        INSERT INTO repo_messages
            (repo_id, channel, username, sender_id, content, context_type, context_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING message_id
    """, (r["repo_id"], f"repo-{name}", user["username"], user["user_id"],
          content, context_type, context_id or None))
    # query_scalar does not commit; get_db closes without committing,
    # which would implicitly rollback the INSERT and leave the WS-broadcast
    # message_id pointing at a phantom row.
    conn.commit()

    message = {
        "message_id": msg_id,
        "username": user["username"],
        "content": content,
        "context_type": context_type,
        "context_id": context_id or "",
        "created_at": __import__("datetime").datetime.now().strftime("%b %d %H:%M"),
    }
    # Broadcast failures must NOT poison the response — the row is already
    # committed and the user's "send" succeeded by every meaningful measure.
    try:
        await mana_manager.broadcast(f"repo-{name}", message)
    except Exception:
        pass

    return {"status": "sent", "message_id": msg_id}


@app.get("/repo/{name}/mana/{message_id}", response_class=HTMLResponse)
def repo_mana_thread_page(name: str, message_id: int, request: Request,
                          conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    root = db.query_one(conn, """
        SELECT m.*, u.username as sender_name FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.message_id = %s AND m.repo_id = %s
    """, (message_id, r["repo_id"]))
    if not root:
        raise HTTPException(404)

    # If the URL points at a reply, redirect to the root so threads always
    # render in canonical form.
    if root.get("parent_id"):
        target = root.get("thread_id") or root["parent_id"]
        return RedirectResponse(
            url=f"/repo/{name}/mana/{target}", status_code=302)

    replies = db.query(conn, """
        SELECT m.*, u.username as sender_name FROM repo_messages m
          LEFT JOIN repo_users u ON u.user_id = m.sender_id
         WHERE m.repo_id = %s
           AND (m.thread_id = %s OR m.parent_id = %s)
         ORDER BY m.created_at ASC
    """, (r["repo_id"], message_id, message_id))

    branches = repo.get_branches(conn, r["repo_id"])
    return templates.TemplateResponse(request, "repo_mana_thread.html", {
        "user": user, "repo": r, "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "tab": "mana",
        "root": root, "replies": replies,
    })


@app.post("/api/repos/{name}/mana/{message_id}/reply")
async def repo_mana_reply(name: str, message_id: int, request: Request,
                          content: str = Form(...),
                          conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    if not repo.check_visibility(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(403)

    # The root (or any message in this repo) must exist before we attach.
    root = db.query_one(conn, """
        SELECT message_id, thread_id, parent_id FROM repo_messages
         WHERE message_id = %s AND repo_id = %s
    """, (message_id, r["repo_id"]))
    if not root:
        raise HTTPException(404)

    # Always thread under the canonical root, even if the caller passed a
    # reply's message_id by mistake.
    thread_root = root.get("thread_id") or root.get("parent_id") or message_id

    reply_id = db.query_scalar(conn, """
        INSERT INTO repo_messages
            (repo_id, channel, username, sender_id, content,
             context_type, parent_id, thread_id)
        VALUES (%s, %s, %s, %s, %s, 'general', %s, %s)
        RETURNING message_id
    """, (r["repo_id"], f"repo-{name}", user["username"], user["user_id"],
          content, thread_root, thread_root))

    db.execute(conn, """
        UPDATE repo_messages
           SET reply_count = reply_count + 1
         WHERE message_id = %s
    """, (thread_root,))

    try:
        await mana_manager.broadcast(f"repo-{name}", {
            "message_id": reply_id,
            "parent_id": thread_root,
            "thread_id": thread_root,
            "username": user["username"],
            "content": content,
            "created_at": __import__("datetime").datetime.now().strftime("%b %d %H:%M"),
        })
    except Exception:
        pass

    return {"status": "sent", "message_id": reply_id, "thread_id": thread_root}


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

    # Direct commits require write permission — owner or an explicit
    # repo_access row at 'write' / 'admin' level. Non-owners contributing
    # to someone else's repo must use the offer/staging flow
    # (POST /api/sync/{name}/offer) which routes changes through
    # Olympian/Zeus review rather than writing the canonical tree.
    if not repo.check_can_write(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(
            403,
            "Direct commits require write access. "
            "Use offer/staging to contribute to this repo.",
        )

    file_data = []
    for f in files:
        content = await f.read()
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
    # Authenticate via session cookie before accepting the connection.
    # Channel format is "repo-<name>" — the caller must be allowed to view
    # that repo (private repos require matching membership).
    session_id = websocket.cookies.get("session_id")
    conn = db.connect()
    try:
        user_id = db.validate_session(conn, session_id) if session_id else None
        user = db.get_user(conn, user_id) if user_id else None
        if not user:
            await websocket.close(code=1008)  # policy violation
            return

        if channel.startswith("repo-"):
            repo_name_val = channel[len("repo-"):]
            r = repo.get_repo(conn, repo_name_val)
            if not r:
                await websocket.close(code=1008)
                return
            if r["visibility"] == "private" and not repo.check_visibility(
                    conn, r["repo_id"], user["user_id"]):
                await websocket.close(code=1008)
                return
        else:
            # Unknown channel shape — refuse rather than subscribe blindly.
            await websocket.close(code=1008)
            return
    finally:
        conn.close()

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

    # Per-key value validation — the old version allowlisted the key but
    # accepted any value, which meant e.g. registration_policy could be
    # set to an arbitrary string and the rest of the codebase would
    # silently fall through to defaults or behave oddly.
    value = value.strip()
    if key == "registration_policy":
        if value not in ("open", "invite_only", "closed"):
            raise HTTPException(400,
                "registration_policy must be one of: open, invite_only, closed")
    elif key == "default_repo_visibility":
        if value not in ("public", "private", "internal"):
            raise HTTPException(400,
                "default_repo_visibility must be one of: public, private, internal")
    elif key == "max_pack_size_mb":
        try:
            mb = int(value)
        except ValueError:
            raise HTTPException(400, "max_pack_size_mb must be an integer")
        if mb < 1 or mb > 10240:
            raise HTTPException(400, "max_pack_size_mb must be between 1 and 10240")
        value = str(mb)
    elif key == "instance_url":
        # Require scheme + host; rely on urlparse since we're not enforcing
        # strict spec compliance, just a sanity gate.
        from urllib.parse import urlparse
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(400,
                "instance_url must be a full http/https URL")
    elif key == "instance_name":
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,62}$", value):
            raise HTTPException(400,
                "instance_name: 1-63 chars, alphanumeric + space/_/./- ")
    else:
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

@app.get("/repo/{name}/access", response_class=HTMLResponse)
def repo_access_page(name: str, request: Request, conn=Depends(get_db)):
    user = require_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if user["role"] != "zeus" and user["user_id"] != r["owner_id"]:
        raise HTTPException(403, "Access denied.")

    access_users = db.get_repo_access_users(conn, r["repo_id"])

    return templates.TemplateResponse(request, "repo_access.html", {
        "user": user,
        "repo": r,
        "access_users": access_users,
    })

@app.post("/api/repos/{name}/access/grant")
def grant_access_api(name: str, request: Request, user_id: int = Form(...),
                     conn=Depends(get_db)):
    user = require_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if user["role"] != "zeus" and user["user_id"] != r["owner_id"]:
        raise HTTPException(403, "Access denied.")

    target_user = db.get_user(conn, user_id)
    if not target_user:
        raise HTTPException(404, "User to grant access to not found.")

    if r["owner_id"] == user_id:
        raise HTTPException(400, "Owner already has access.")

    db.grant_repo_access(conn, r["repo_id"], user_id, user["user_id"])
    return {"status": "granted", "user_id": user_id}

@app.delete("/api/repos/{name}/access/{user_id}")
def revoke_access_api(name: str, user_id: int, request: Request,
                      conn=Depends(get_db)):
    user = require_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if user["role"] != "zeus" and user["user_id"] != r["owner_id"]:
        raise HTTPException(403, "Access denied.")

    if r["owner_id"] == user_id:
        raise HTTPException(400, "Cannot revoke access from the repository owner.")

    db.revoke_repo_access(conn, r["repo_id"], user_id, user["user_id"])
    return {"status": "revoked", "user_id": user_id}


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

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
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
        SELECT * FROM repo_commits 
        WHERE commit_hash LIKE %s AND repo_id = %s
    """, (commit_hash + '%', r["repo_id"]))
    if not commit_row:
        raise HTTPException(404, "Commit not found.")

    changesets = db.query(conn, """
        SELECT * FROM repo_changesets WHERE commit_hash = %s ORDER BY path
    """, (commit_row["commit_hash"],))

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
def request_password_reset(request: Request,
                           email: str = Form(...), conn=Depends(get_db)):
    # Rate limit: 3 reset requests per IP per hour. Limits email-based
    # user enumeration (attacker checking whether an email is registered).
    _rate_limit(request, "request_reset", max_hits=3, window_sec=3600)

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

    # Token is only delivered via the server console (admin-relayed) or email.
    # Never include it in the HTTP response — any caller with the target email
    # would otherwise be able to reset that account's password.
    print(f"PASSWORD RESET TOKEN for {user['username']}: /reset-password?token={token}")
    return {
        "status": "ok",
        "message": "If that email exists, a reset token has been generated.",
    }

@app.post("/api/auth/reset-password")
def do_password_reset(request: Request,
                      token: str = Form(...),
                      new_password: str = Form(...),
                      conn=Depends(get_db)):
    # Rate limit: 5 attempts per IP per 15 minutes. Same class of brute-
    # force protection as login — reset tokens shouldn't be guessable.
    _rate_limit(request, "reset_password", max_hits=5, window_sec=900)

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
    # Clear notification badge for this message thread
    db.execute(conn, """
        UPDATE repo_notifications
           SET is_read = TRUE
         WHERE user_id = %s
           AND (link LIKE %s OR link LIKE %s)
    """, (user["user_id"],
          f"%/messages/{message_id}%",
          f"%/messages/thread/{message_id}%"))

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

    # Visibility gate — without this, any authenticated user could comment
    # on commits in private repos they can't even browse. Matches the
    # /api/repos/{name}/comments endpoint's gate at line 2148.
    if not repo.check_visibility(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(403)

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


@app.get("/api/repos/{name}/commits/{commit_hash}/diff")
def commit_diff_api(name: str, commit_hash: str, 
                    request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(403)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"], user["user_id"]):
        raise HTTPException(403)

    commit_row = db.query_one(conn, """
        SELECT * FROM repo_commits WHERE commit_hash = %s AND repo_id = %s
    """, (commit_hash, r["repo_id"]))
    if not commit_row:
        raise HTTPException(404)

    changes = db.query(conn, """
        SELECT path, change_type, blob_before, blob_after,
               lines_added, lines_removed
          FROM repo_changesets
         WHERE commit_hash = %s ORDER BY path
    """, (commit_hash,))

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store
    from ..core import diff as diff_mod

    result = []
    for change in changes:
        entry = {
            "path":          change["path"],
            "change_type":   change["change_type"],
            "lines_added":   change["lines_added"],
            "lines_removed": change["lines_removed"],
            "diff":          None,
            "is_binary":     False,
        }

        def get_content(blob_hash):
            if not blob_hash:
                return "", False
            b = obj_store.retrieve_blob(blob_hash, objects_dir)
            if b:
                try:
                    return b.decode("utf-8"), False
                except UnicodeDecodeError:
                    return None, True
            return "", False

        if change["change_type"] == "modify":
            old_content, old_binary = get_content(change["blob_before"])
            new_content, new_binary = get_content(change["blob_after"])
            if old_binary or new_binary:
                entry["is_binary"] = True
            else:
                entry["diff"] = diff_mod.diff_side_by_side(old_content, new_content)
        elif change["change_type"] == "add":
            new_content, is_binary = get_content(change["blob_after"])
            entry["is_binary"] = is_binary
            if not is_binary:
                entry["diff"] = diff_mod.diff_side_by_side("", new_content)
        elif change["change_type"] == "delete":
            old_content, is_binary = get_content(change["blob_before"])
            entry["is_binary"] = is_binary
            if not is_binary:
                entry["diff"] = diff_mod.diff_side_by_side(old_content, "")

        result.append(entry)

    return result


# =========================================================================
# ISSUE TRACKER
# =========================================================================

def _get_next_issue_number(conn, repo_id: int) -> int:
    return db.query_scalar(conn,
        "SELECT COALESCE(MAX(number), 0) + 1 FROM repo_issues WHERE repo_id = %s",
        (repo_id,)) or 1


def _parse_issue_refs(message: str) -> list[tuple[int, str]]:
    """
    Parse issue references from commit message.
    Supports: fixes #N, closes #N, resolves #N, relates #N
    Returns list of (issue_number, link_type)
    """
    import re
    refs = []
    patterns = [
        (r'(?:fixes|fix|closes|close|resolves|resolve)\s+#(\d+)', 'fixed'),
        (r'(?:introduces|introduced)\s+#(\d+)', 'introduced'),
        (r'(?:relates|related|see)\s+#(\d+)', 'related'),
        (r'#(\d+)', 'mentioned'),
    ]
    seen = set()
    for pattern, link_type in patterns:
        for match in re.finditer(pattern, message, re.IGNORECASE):
            num = int(match.group(1))
            if num not in seen:
                refs.append((num, link_type))
                seen.add(num)
    return refs


@app.get("/repo/{name}/issues", response_class=HTMLResponse)
def issues_page(name: str, request: Request,
                status: str = "open",
                issue_type: str = "",
                priority: str = "",
                assigned: str = "",
                conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    filters = ["i.repo_id = %s"]
    params  = [r["repo_id"]]

    if status:
        filters.append("i.status = %s")
        params.append(status)
    if issue_type:
        filters.append("i.issue_type = %s")
        params.append(issue_type)
    if priority:
        filters.append("i.priority = %s")
        params.append(priority)
    if assigned:
        filters.append("u2.username = %s")
        params.append(assigned)

    where = " AND ".join(filters)
    issues = db.query(conn, f"""
        SELECT i.*, 
               u1.username as reporter_name,
               u2.username as assignee_name,
               COUNT(ic.comment_id) as comment_count
          FROM repo_issues i
          LEFT JOIN repo_users u1 ON u1.user_id = i.reported_by
          LEFT JOIN repo_users u2 ON u2.user_id = i.assigned_to
          LEFT JOIN repo_issue_comments ic ON ic.issue_id = i.issue_id
         WHERE {where}
         GROUP BY i.issue_id, u1.username, u2.username
         ORDER BY
             CASE i.priority
                 WHEN 'critical' THEN 1
                 WHEN 'high'     THEN 2
                 WHEN 'normal'   THEN 3
                 WHEN 'low'      THEN 4
             END,
             i.updated_at DESC
    """, params)

    # Counts for filter badges
    counts = db.query(conn, """
        SELECT status, COUNT(*) as n
          FROM repo_issues WHERE repo_id = %s
         GROUP BY status
    """, (r["repo_id"],))
    status_counts = {c["status"]: c["n"] for c in counts}

    branches = repo.get_branches(conn, r["repo_id"])
    all_users = db.query(conn, """
        SELECT user_id, username, role FROM repo_users
         WHERE is_active = TRUE ORDER BY username
    """)

    return templates.TemplateResponse(request, "repo_issues.html", {
        "user": user, "repo": r, "issues": issues,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
        "filter_status": status,
        "filter_type": issue_type,
        "filter_priority": priority,
        "filter_assigned": assigned,
        "status_counts": status_counts,
        "all_users": all_users,
    })


@app.get("/repo/{name}/issues/new", response_class=HTMLResponse)
def new_issue_page(name: str, request: Request,
                   file: str = "", line: int = 0,
                   conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    all_users = db.query(conn, """
        SELECT user_id, username, role FROM repo_users
         WHERE is_active = TRUE ORDER BY username
    """)

    return templates.TemplateResponse(request, "new_issue.html", {
        "user": user, "repo": r,
        "all_users": all_users,
        "prefill_file": file,
        "prefill_line": line,
    })


@app.post("/api/repos/{name}/issues")
def create_issue(name: str, request: Request,
                 title: str = Form(...),
                 description: str = Form(""),
                 issue_type: str = Form("bug"),
                 priority: str = Form("normal"),
                 assigned_to: int = Form(None),
                 file_path: str = Form(""),
                 line_start: int = Form(None),
                 line_end: int = Form(None),
                 conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    try:
        number = _get_next_issue_number(conn, r["repo_id"])

        issue_id = db.query_scalar(conn, """
            INSERT INTO repo_issues
                (repo_id, number, title, description,
                 issue_type, priority, reported_by, assigned_to)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING issue_id
        """, (r["repo_id"], number, title.strip(),
              description.strip() or None,
              issue_type, priority,
              user["user_id"], assigned_to or None))

        # Attach file if provided
        if file_path.strip():
            db.execute(conn, """
                INSERT INTO repo_issue_files
                    (issue_id, path, line_start, line_end)
                VALUES (%s, %s, %s, %s)
            """, (issue_id, file_path.strip(),
                  line_start or None, line_end or None),
                commit=False)

        # Notify assignee
        if assigned_to and assigned_to != user["user_id"]:
            db.create_notification(
                conn,
                user_id=assigned_to,
                notif_type="issue_assigned",
                message=f"{user['username']} assigned issue #{number} to you: {title}",
                link=f"/repo/{name}/issues/{number}",
                repo_id=r["repo_id"],
                commit=False
            )

        db.audit_log(conn, "issue_create", user_id=user["user_id"],
                     repo_id=r["repo_id"], target_type="issue",
                     target_id=str(number),
                     details={"title": title, "type": issue_type},
                     commit=False)
        conn.commit()
        return {"status": "created", "issue_id": issue_id, "number": number}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@app.get("/repo/{name}/issues/{number}", response_class=HTMLResponse)
def issue_detail_page(name: str, number: int,
                      request: Request, conn=Depends(get_db)):
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    issue = db.query_one(conn, """
        SELECT i.*,
               u1.username as reporter_name,
               u2.username as assignee_name
          FROM repo_issues i
          LEFT JOIN repo_users u1 ON u1.user_id = i.reported_by
          LEFT JOIN repo_users u2 ON u2.user_id = i.assigned_to
         WHERE i.repo_id = %s AND i.number = %s
    """, (r["repo_id"], number))
    if not issue:
        raise HTTPException(404, f"Issue #{number} not found.")

    comments = db.query(conn, """
        SELECT ic.*, u.username, u.role
          FROM repo_issue_comments ic
          LEFT JOIN repo_users u ON u.user_id = ic.user_id
         WHERE ic.issue_id = %s
         ORDER BY ic.created_at ASC
    """, (issue["issue_id"],))

    files = db.query(conn, """
        SELECT * FROM repo_issue_files
         WHERE issue_id = %s
    """, (issue["issue_id"],))

    linked_commits = db.query(conn, """
        SELECT ic.link_type, c.commit_hash, c.rev,
               c.message, c.author_name, c.committed_at
          FROM repo_issue_commits ic
          JOIN repo_commits c ON c.commit_hash = ic.commit_hash
         WHERE ic.issue_id = %s
         ORDER BY c.committed_at DESC
    """, (issue["issue_id"],))

    all_users = db.query(conn, """
        SELECT user_id, username, role FROM repo_users
         WHERE is_active = TRUE ORDER BY username
    """)

    return templates.TemplateResponse(request, "issue_detail.html", {
        "user": user, "repo": r, "issue": issue,
        "comments": comments, "files": files,
        "linked_commits": linked_commits,
        "all_users": all_users,
    })


@app.post("/api/repos/{name}/issues/{number}/comments")
def add_issue_comment(name: str, number: int,
                      request: Request,
                      content: str = Form(...),
                      conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    issue = db.query_one(conn,
        "SELECT * FROM repo_issues WHERE repo_id = %s AND number = %s",
        (r["repo_id"], number))
    if not issue:
        raise HTTPException(404)

    try:
        comment_id = db.query_scalar(conn, """
            INSERT INTO repo_issue_comments (issue_id, user_id, content)
            VALUES (%s, %s, %s) RETURNING comment_id
        """, (issue["issue_id"], user["user_id"], content.strip()))

        db.execute(conn,
            "UPDATE repo_issues SET updated_at = NOW() WHERE issue_id = %s",
            (issue["issue_id"],), commit=False)

        # Notify reporter and assignee
        notify_users = set()
        if issue["reported_by"] and issue["reported_by"] != user["user_id"]:
            notify_users.add(issue["reported_by"])
        if issue["assigned_to"] and issue["assigned_to"] != user["user_id"]:
            notify_users.add(issue["assigned_to"])

        for uid in notify_users:
            db.create_notification(
                conn, user_id=uid,
                notif_type="issue_comment",
                message=f"{user['username']} commented on issue #{number}: {issue['title']}",
                link=f"/repo/{name}/issues/{number}",
                repo_id=r["repo_id"],
                commit=False
            )

        conn.commit()
        return {"status": "added", "comment_id": comment_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@app.patch("/api/repos/{name}/issues/{number}")
def update_issue(name: str, number: int,
                 request: Request,
                 status: str = Form(None),
                 assigned_to: int = Form(None),
                 priority: str = Form(None),
                 conn=Depends(get_db)):
    user = get_current_user(request, conn)
    if not user:
        raise HTTPException(401)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    issue = db.query_one(conn,
        "SELECT * FROM repo_issues WHERE repo_id = %s AND number = %s",
        (r["repo_id"], number))
    if not issue:
        raise HTTPException(404)

    updates = []
    params  = []

    if status:
        valid = ('open','in_progress','resolved','closed','wontfix')
        if status not in valid:
            raise HTTPException(400, "Invalid status.")
        updates.append("status = %s")
        params.append(status)
        if status in ('resolved', 'closed'):
            updates.append("closed_at = NOW()")

    if priority:
        updates.append("priority = %s")
        params.append(priority)

    if assigned_to is not None:
        updates.append("assigned_to = %s")
        params.append(assigned_to or None)

    if not updates:
        raise HTTPException(400, "Nothing to update.")

    updates.append("updated_at = NOW()")
    params.append(issue["issue_id"])

    db.execute(conn,
        f"UPDATE repo_issues SET {', '.join(updates)} WHERE issue_id = %s",
        params, commit=False)

    db.audit_log(conn, "issue_update", user_id=user["user_id"],
                 repo_id=r["repo_id"], target_type="issue",
                 target_id=str(number),
                 details={"status": status, "priority": priority},
                 commit=False)
    conn.commit()
    return {"status": "updated"}


# =========================================================================
# SYNC API — endpoints canonical exposes to slaves
# =========================================================================

@app.get("/api/sync/{name}/info")
def sync_info(name: str, request: Request, conn=Depends(get_db)):
    """
    Public sync info endpoint.
    Slaves call this to find out the canonical rev and repo metadata.
    """
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if r["visibility"] == "private":
        user = get_current_user(request, conn)
        if not user:
            raise HTTPException(403)

    latest = db.query_one(conn, """
        SELECT c.rev, c.commit_hash, c.committed_at
          FROM repo_commits c
         WHERE c.repo_id = %s AND c.rev IS NOT NULL
         ORDER BY c.rev DESC LIMIT 1
    """, (r["repo_id"],))

    return {
        "repo_name":      r["name"],
        "repo_id":        r["repo_id"],
        "visibility":     r["visibility"],
        "default_branch": r["default_branch"],
        "latest_rev":     latest["rev"] if latest else 0,
        "latest_hash":    latest["commit_hash"] if latest else None,
    }


@app.get("/api/sync/{name}/commits")
def sync_commits(name: str, request: Request,
                 since_rev: int = 0,
                 conn=Depends(get_db)):
    """
    Returns all commits after since_rev.
    Slaves call this during pull to get new commits.
    """
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if r["visibility"] == "private":
        user = get_current_user(request, conn)
        if not user:
            raise HTTPException(403)

    commits = db.query(conn, """
        SELECT c.rev, c.commit_hash, c.tree_hash,
               c.author_name, c.committer_name,
               c.message, c.committed_at, c.parent_hashes
          FROM repo_commits c
         WHERE c.repo_id = %s AND c.rev > %s
         ORDER BY c.rev ASC
    """, (r["repo_id"], since_rev))

    # Include changesets for each commit
    result = []
    for c in commits:
        changesets = db.query(conn, """
            SELECT path, change_type, blob_before, blob_after,
                   lines_added, lines_removed
              FROM repo_changesets
             WHERE commit_hash = %s
        """, (c["commit_hash"],))
        result.append({**dict(c), "changesets": list(changesets)})

    return result


@app.get("/api/sync/{name}/blob/{blob_hash}")
def sync_blob(name: str, blob_hash: str,
              request: Request, conn=Depends(get_db)):
    """
    Serve a blob by hash.
    Slaves call this to fetch blob content they don't have locally.
    """
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if r["visibility"] == "private":
        user = get_current_user(request, conn)
        if not user:
            raise HTTPException(403)

    # Validate hash format
    if not re.match(r'^[a-f0-9]{64}$', blob_hash):
        raise HTTPException(400, "Invalid blob hash.")

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store
    content = obj_store.retrieve_blob(blob_hash, objects_dir)
    if content is None:
        raise HTTPException(404, "Blob not found.")

    from fastapi.responses import Response
    return Response(content=content, media_type="application/octet-stream")


# =========================================================================
# OFFER — slave submits work to canonical for review
# =========================================================================

MAX_OFFER_BLOB_BYTES = 100 * 1024 * 1024  # 100 MiB total per offer
MAX_OFFER_CHANGES    = 10_000


@app.post("/api/sync/{name}/offer")
async def receive_offer(name: str, request: Request,
                        conn=Depends(get_db)):
    """
    Receive an offer from a slave instance.
    Creates a staging realm on canonical for Zeus/Olympian review.
    Does NOT write to canonical tree — offer must be promoted.

    Requires a signed envelope:
        { "payload": { ...offer fields..., "public_key": "<hex>",
                       "timestamp": <unix> },
          "signature": "<hex ed25519>" }
    The signer's public_key identifies the remote contributor; the claimed
    ``offered_by`` label is informational only.
    """
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    envelope = await request.json()
    from ..core import identity as id_mod

    payload = id_mod.verify_envelope(envelope)
    if not payload:
        raise HTTPException(401, "Offer envelope missing or signature invalid.")

    signer_pub  = payload["public_key"]             # authenticated
    branch_name = payload.get("branch_name", "offered")
    from_rev    = payload.get("from_rev", 0)
    base_rev    = payload.get("base_rev", 0)
    claimed_by  = str(payload.get("offered_by") or "")[:64]
    message     = str(payload.get("message", ""))[:4000]
    changes     = payload.get("changes", [])
    blobs       = payload.get("blobs", {})  # {hash: base64_content}

    if not changes:
        raise HTTPException(400, "No changes in offer.")
    if not isinstance(changes, list) or len(changes) > MAX_OFFER_CHANGES:
        raise HTTPException(400, "Too many changes in offer.")

    # Approximate size from base64 payload (4 b64 chars ≈ 3 bytes).
    if isinstance(blobs, dict):
        approx_blob_bytes = sum(len(v) for v in blobs.values()) * 3 // 4
        if approx_blob_bytes > MAX_OFFER_BLOB_BYTES:
            raise HTTPException(413, "Offer blob payload too large.")
    else:
        blobs = {}

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store

    try:
        # Store any blobs we don't have yet (hash-verified by store_blob below)
        for blob_hash, b64_content in blobs.items():
            if not isinstance(blob_hash, str) or not re.match(r'^[a-f0-9]{64}$', blob_hash):
                raise HTTPException(400, "Invalid blob hash format.")
            if not obj_store.exists(blob_hash, objects_dir):
                content = base64.b64decode(b64_content)
                stored_hash = obj_store.store_blob(content, objects_dir)
                if stored_hash != blob_hash:
                    raise HTTPException(400, f"Blob hash mismatch: {blob_hash}")

        # The authenticated label includes the signer's pubkey prefix, so two
        # different signers can't collide on one claimed username.
        offered_by = f"{claimed_by or 'remote'}@{signer_pub[:12]}"

        # Create offer record
        offer_id = db.query_scalar(conn, """
            INSERT INTO repo_offers
                (repo_id, branch_name, from_rev, base_rev,
                 offered_by, message, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            RETURNING offer_id
        """, (r["repo_id"], branch_name, from_rev,
              base_rev, offered_by, message))

        # Store offer changes
        for change in changes:
            db.execute(conn, """
                INSERT INTO repo_offer_changes
                    (offer_id, path, change_type, blob_hash,
                     lines_added, lines_removed)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (offer_id, change["path"], change["change_type"],
                  change.get("blob_hash"), change.get("lines_added", 0),
                  change.get("lines_removed", 0)), commit=False)

        # Staging realm also keyed by the (label@pubkey) identity so the
        # ghost user is unambiguous and cannot hijack a real username.
        offering_user = db.get_user_by_name(conn, offered_by)
        if offering_user:
            staging_user_id = offering_user["user_id"]
        else:
            # Remote contributor: lowest privilege, inactive (can't log in).
            # The placeholder password hash is not a valid bcrypt string,
            # so crypt() comparison in the login path will never match.
            staging_user_id = db.query_scalar(conn, """
                INSERT INTO repo_users
                    (username, password_hash, role, is_active)
                VALUES (%s, 'remote-contributor-no-login', 'mortal', FALSE)
                ON CONFLICT (username) DO UPDATE
                    SET username = EXCLUDED.username
                RETURNING user_id
            """, (offered_by,))

        staging_id = db.query_scalar(conn, """
            INSERT INTO repo_staging
                (repo_id, user_id, branch_name, status)
            VALUES (%s, %s, %s, 'active')
            ON CONFLICT (repo_id, user_id, branch_name)
            DO UPDATE SET status = 'active', updated_at = NOW()
            RETURNING staging_id
        """, (r["repo_id"], staging_user_id, branch_name))

        for change in changes:
            db.execute(conn, """
                INSERT INTO repo_staging_changes
                    (staging_id, path, change_type, blob_hash,
                     lines_added, lines_removed)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (staging_id, change["path"], change["change_type"],
                  change.get("blob_hash"), change.get("lines_added", 0),
                  change.get("lines_removed", 0)), commit=False)

        # Notify Zeus
        zeus_users = db.query(conn,
            "SELECT user_id FROM repo_users WHERE role = 'zeus' AND is_active = TRUE")
        for z in zeus_users:
            db.create_notification(
                conn, user_id=z["user_id"],
                notif_type="offer_received",
                message=f"New offer from {offered_by} on {name}: {message[:80]}",
                link=f"/repo/{name}/staging/{staging_id}",
                repo_id=r["repo_id"],
                commit=False
            )

        db.audit_log(conn, "offer_received", repo_id=r["repo_id"],
                     target_type="offer", target_id=str(offer_id),
                     details={"offered_by": offered_by,
                              "files": len(changes)}, commit=False)
        conn.commit()

        return {
            "status":     "received",
            "offer_id":   offer_id,
            "staging_id": staging_id,
            "message":    f"Offer received. {len(changes)} file(s) pending review."
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# =========================================================================
# STAGING DIFF — side by side diff for offer review
# =========================================================================

@app.get("/api/repos/{name}/staging/{staging_id}/diff")
def staging_diff_api(name: str, staging_id: int,
                     request: Request, conn=Depends(get_db)):
    """
    Returns side-by-side diff for each changed file in a staging realm.
    Compares canonical HEAD vs offered version.
    """
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    staging_row = db.query_one(conn,
        "SELECT * FROM repo_staging WHERE staging_id = %s AND repo_id = %s",
        (staging_id, r["repo_id"]))
    if not staging_row:
        raise HTTPException(404)

    changes = db.query(conn, """
        SELECT path, change_type, blob_hash,
               lines_added, lines_removed
          FROM repo_staging_changes
         WHERE staging_id = %s ORDER BY path
    """, (staging_id,))

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store
    from ..core import diff as diff_mod

    result = []
    for change in changes:
        entry = {
            "path":          change["path"],
            "change_type":   change["change_type"],
            "lines_added":   change["lines_added"],
            "lines_removed": change["lines_removed"],
            "diff":          None,
            "is_binary":     False,
        }

        def get_canonical_content(path):
            row = db.query_one(conn, """
                SELECT cs.blob_after FROM repo_changesets cs
                  JOIN repo_commits c ON c.commit_hash = cs.commit_hash
                  JOIN repo_refs rf ON rf.commit_hash = c.commit_hash
                 WHERE c.repo_id = %s
                   AND rf.ref_name = %s
                   AND cs.path = %s
                   AND cs.change_type != 'delete'
                 ORDER BY c.rev DESC LIMIT 1
            """, (r["repo_id"],
                  f"refs/heads/{r.get('default_branch','main')}",
                  path))
            if row and row["blob_after"]:
                b = obj_store.retrieve_blob(row["blob_after"], objects_dir)
                if b:
                    try:
                        return b.decode("utf-8"), False
                    except UnicodeDecodeError:
                        return None, True
            return "", False

        def get_offered_content(blob_hash):
            if not blob_hash:
                return "", False
            b = obj_store.retrieve_blob(blob_hash, objects_dir)
            if b:
                try:
                    return b.decode("utf-8"), False
                except UnicodeDecodeError:
                    return None, True
            return "", False

        if change["change_type"] == "modify":
            old_content, old_binary = get_canonical_content(change["path"])
            new_content, new_binary = get_offered_content(change["blob_hash"])
            if old_binary or new_binary:
                entry["is_binary"] = True
            else:
                entry["diff"] = diff_mod.diff_side_by_side(
                    old_content, new_content)

        elif change["change_type"] == "add":
            new_content, is_binary = get_offered_content(change["blob_hash"])
            if is_binary:
                entry["is_binary"] = True
            else:
                entry["diff"] = diff_mod.diff_side_by_side("", new_content)

        elif change["change_type"] == "delete":
            old_content, is_binary = get_canonical_content(change["path"])
            if is_binary:
                entry["is_binary"] = True
            else:
                entry["diff"] = diff_mod.diff_side_by_side(old_content, "")

        result.append(entry)

    return result


@app.get("/repo/{name}/staging/{staging_id}/review",
         response_class=HTMLResponse)
def staging_review_page(name: str, staging_id: int,
                        request: Request, conn=Depends(get_db)):
    """Full side-by-side review page for a staging realm."""
    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    staging_row = db.query_one(conn, """
        SELECT s.*, u.username, u.role as user_role
          FROM repo_staging s
          JOIN repo_users u ON u.user_id = s.user_id
         WHERE s.staging_id = %s AND s.repo_id = %s
    """, (staging_id, r["repo_id"]))
    if not staging_row:
        raise HTTPException(404)

    changes = db.query(conn, """
        SELECT path, change_type, blob_hash,
               lines_added, lines_removed
          FROM repo_staging_changes
         WHERE staging_id = %s ORDER BY path
    """, (staging_id,))

    branches = repo.get_branches(conn, r["repo_id"])

    return templates.TemplateResponse(request, "staging_review.html", {
        "user": user, "repo": r,
        "staging": staging_row,
        "changes": changes,
        "branches": branches,
        "current_branch": r.get("default_branch", "main"),
    })


@app.post("/api/repos/{name}/promote/{staging_id}")
def promote_staging(name: str, staging_id: int,
                    request: Request,
                    notes: str = Form(""),
                    conn=Depends(get_db)):
    if not notes or not notes.strip():
        raise HTTPException(400, "Review notes are required")

    user = get_current_user(request, conn)
    if not user or user["role"] not in ("zeus", "olympian"):
        raise HTTPException(403)

    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)

    staging_row = db.query_one(conn,
        "SELECT * FROM repo_staging WHERE staging_id = %s AND repo_id = %s",
        (staging_id, r["repo_id"]))
    if not staging_row:
        raise HTTPException(404)
    if staging_row["status"] != "active":
        raise HTTPException(400, "This staging realm is not active.")

    changes = db.query(conn,
        "SELECT * FROM repo_staging_changes WHERE staging_id = %s",
        (staging_id,))
    if not changes:
        raise HTTPException(400, "No changes to promote.")

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store
    import time

    branch = staging_row["branch_name"]
    owner = db.get_user(conn, staging_row["user_id"])

    parent_row = db.query_one(conn,
        "SELECT commit_hash FROM repo_refs WHERE repo_id = %s AND ref_name = %s",
        (r["repo_id"], f"refs/heads/{r.get('default_branch','main')}"))
    parent_hash = parent_row["commit_hash"] if parent_row else None

    tree_content = json_mod.dumps(
        {c["path"]: c["blob_hash"] for c in changes
         if c["change_type"] != "delete"},
        sort_keys=True
    ).encode()
    tree_hash = obj_store.hash_content(tree_content)
    obj_store.store_blob(tree_content, objects_dir)

    ts = str(time.time())
    msg = f"Promote: {branch}"
    if notes:
        msg += f"\n\n{notes}"
    commit_content = f"{tree_hash}\n{parent_hash or 'none'}\n{owner['username'] if owner else 'unknown'}\n{ts}\n{msg}"
    commit_hash = obj_store.hash_content(commit_content.encode())
    parent_hashes = [parent_hash] if parent_hash else None

    try:
        next_rev = db.query_scalar(conn, 
            "SELECT COALESCE(MAX(rev), 0) + 1 FROM repo_commits WHERE repo_id = %s",
            (r["repo_id"],))

        db.execute(conn, """
            INSERT INTO repo_commits
                (commit_hash, repo_id, tree_hash, author_id, author_name,
                 committer_id, committer_name, message, parent_hashes, rev)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (commit_hash, r["repo_id"], tree_hash,
              staging_row["user_id"],
              owner["username"] if owner else "unknown",
              user["user_id"], user["username"],
              msg, parent_hashes, next_rev), commit=False)

        for c in changes:
            db.execute(conn, """
                INSERT INTO repo_changesets
                    (commit_hash, path, change_type, blob_before,
                     blob_after, lines_added, lines_removed)
                VALUES (%s, %s, %s, NULL, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (commit_hash, c["path"], c["change_type"],
                  c["blob_hash"], c["lines_added"],
                  c["lines_removed"]), commit=False)

        db.execute(conn, """
            UPDATE repo_refs
               SET commit_hash = %s, updated_at = NOW(), updated_by = %s
             WHERE repo_id = %s AND ref_name = %s
        """, (commit_hash, user["user_id"], r["repo_id"],
              f"refs/heads/{r.get('default_branch','main')}"),
            commit=False)

        db.execute(conn, """
            INSERT INTO repo_promotions
                (staging_id, repo_id, promoted_by, commit_hash, notes)
            VALUES (%s, %s, %s, %s, %s)
        """, (staging_id, r["repo_id"], user["user_id"],
              commit_hash, notes or None), commit=False)

        db.execute(conn,
            "UPDATE repo_staging SET status = 'promoted', updated_at = NOW() WHERE staging_id = %s",
            (staging_id,), commit=False)

        db.audit_log(conn, "promote", user_id=user["user_id"],
                     repo_id=r["repo_id"], target_type="staging",
                     target_id=str(staging_id),
                     details={"commit_hash": commit_hash, "notes": notes},
                     commit=False)

        db.execute(conn,
            "UPDATE repo_repositories SET updated_at = NOW() WHERE repo_id = %s",
            (r["repo_id"],), commit=False)

        conn.commit()

        rev = db.query_scalar(conn,
            "SELECT rev FROM repo_commits WHERE commit_hash = %s",
            (commit_hash,))

        return {"status": "promoted", "commit_hash": commit_hash, "rev": rev}

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Promotion failed: {e}")


# =========================================================================
# TRIBUTE — Web-based contribution entry point
#
# Discoverable path for drive-by contributors: bug report, discussion,
# single-file edit, or multi-file patch upload. All four terminate in
# existing tables (repo_issues, repo_messages/mana, repo_staging).
# =============================================================================

import time
import zipfile
import io


# --- Limits (tune to taste) ---------------------------------------------
TRIBUTE_MAX_FILE_BYTES    = 1 * 1024 * 1024     # 1 MB per file in a patch
TRIBUTE_MAX_PATCH_BYTES   = 5 * 1024 * 1024     # 5 MB total zip upload
TRIBUTE_MAX_PATCH_FILES   = 50                  # max files in one patch
TRIBUTE_MIN_REASON_CHARS  = 10


def _tribute_visibility_check(conn, r, user):
    """Tribute requires repo-visible + logged in. Mortals may tribute."""
    if not user:
        raise HTTPException(401, "Login required to pay tribute.")
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"]):
        raise HTTPException(403)


# =========================================================================
# TRIBUTE JUNCTION PAGE — choose your path
# =========================================================================

@app.get("/repo/{name}/tribute", response_class=HTMLResponse)
def tribute_page(name: str, request: Request, conn=Depends(get_db)):
    """The tribute junction. Four paths, one screen."""
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    if not repo.check_visibility(conn, r["repo_id"],
                                 user["user_id"] if user else None):
        raise HTTPException(403)

    # List files for the edit picker (top-level and nested)
    branch = r.get("default_branch", "main")
    files = _load_file_tree(conn, r["repo_id"], branch)

    # Only text-ish files are edit candidates
    edit_candidates = [f for f in files if f["type"] == "file"]

    return templates.TemplateResponse(request, "tribute.html", {
        "user": user, "repo": r,
        "edit_candidates": edit_candidates,
        "needs_login": user is None,
    })


# =========================================================================
# EDIT — single file web editor
# =========================================================================

@app.get("/repo/{name}/tribute/edit/{path:path}", response_class=HTMLResponse)
def tribute_edit_page(name: str, path: str, request: Request,
                      conn=Depends(get_db)):
    """Render the in-browser edit page for one file."""
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    _tribute_visibility_check(conn, r, user)

    branch = r.get("default_branch", "main")
    ref_name = f"refs/heads/{branch}"

    row = db.query_one(conn, """
        SELECT cs.blob_after FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
          JOIN repo_refs rf ON rf.commit_hash = c.commit_hash
         WHERE c.repo_id = %s
           AND rf.ref_name = %s
           AND cs.path = %s
           AND cs.change_type != 'delete'
         ORDER BY c.rev DESC LIMIT 1
    """, (r["repo_id"], ref_name, path))

    if not row or not row["blob_after"]:
        raise HTTPException(404, "File not found on this branch.")

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store
    content_bytes = obj_store.retrieve_blob(row["blob_after"], objects_dir)
    if content_bytes is None:
        raise HTTPException(404, "Blob missing from object store.")

    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Binary files can't be edited in the browser.")

    return templates.TemplateResponse(request, "tribute_edit.html", {
        "user": user, "repo": r, "path": path, "branch": branch,
        "content": content,
    })


@app.post("/api/repos/{name}/tribute/edit")
async def tribute_edit_api(name: str, request: Request,
                           conn=Depends(get_db)):
    """Submit a single-file edit as a staging offer."""
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    _tribute_visibility_check(conn, r, user)

    body = await request.json()
    path        = str(body.get("path", "")).strip()
    new_content = body.get("new_content", "")
    reason      = str(body.get("reason", "")).strip()

    if not path:
        raise HTTPException(400, "Missing path.")
    if not isinstance(new_content, str):
        raise HTTPException(400, "Content must be a string.")
    if len(reason) < TRIBUTE_MIN_REASON_CHARS:
        raise HTTPException(400,
            f"Reason must be at least {TRIBUTE_MIN_REASON_CHARS} characters.")
    if len(new_content.encode("utf-8")) > TRIBUTE_MAX_FILE_BYTES:
        raise HTTPException(413, "File too large for web edit.")

    # Fetch current canonical blob to detect no-op
    branch = r.get("default_branch", "main")
    ref_name = f"refs/heads/{branch}"
    row = db.query_one(conn, """
        SELECT cs.blob_after FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
          JOIN repo_refs rf ON rf.commit_hash = c.commit_hash
         WHERE c.repo_id = %s AND rf.ref_name = %s
           AND cs.path = %s AND cs.change_type != 'delete'
         ORDER BY c.rev DESC LIMIT 1
    """, (r["repo_id"], ref_name, path))

    if not row or not row["blob_after"]:
        raise HTTPException(404, "File not found on this branch.")

    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store
    from ..core import diff as diff_mod

    old_bytes = obj_store.retrieve_blob(row["blob_after"], objects_dir)
    if old_bytes is None:
        raise HTTPException(500, "Canonical blob missing from store.")

    try:
        old_text = old_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Binary files can't be edited in the browser.")

    if old_text == new_content:
        raise HTTPException(400, "No changes detected.")

    # Store the new blob
    new_bytes = new_content.encode("utf-8")
    new_hash = obj_store.store_blob(new_bytes, objects_dir)

    # Count line deltas for display
    summary = diff_mod.diff_summary(old_text, new_content)

    try:
        branch_name = f"tribute-{user['username']}-{int(time.time())}"

        staging_id = db.query_scalar(conn, """
            INSERT INTO repo_staging
                (repo_id, user_id, branch_name, status)
            VALUES (%s, %s, %s, 'active')
            RETURNING staging_id
        """, (r["repo_id"], user["user_id"], branch_name))

        db.execute(conn, """
            INSERT INTO repo_staging_changes
                (staging_id, path, change_type, blob_hash,
                 lines_added, lines_removed)
            VALUES (%s, %s, 'modify', %s, %s, %s)
        """, (staging_id, path, new_hash,
              summary["added"], summary["removed"]), commit=False)

        # Notify zeus/olympians
        reviewers = db.query(conn,
            "SELECT user_id FROM repo_users "
            "WHERE role IN ('zeus', 'olympian') AND is_active = TRUE")
        for rev in reviewers:
            db.create_notification(
                conn, user_id=rev["user_id"],
                notif_type="offer_received",
                message=f"Web tribute from {user['username']} on {name}: "
                        f"{reason[:80]}",
                link=f"/repo/{name}/staging/{staging_id}",
                repo_id=r["repo_id"],
                commit=False
            )

        # Audit
        db.audit_log(conn, "tribute_edit",
                     user_id=user["user_id"], repo_id=r["repo_id"],
                     target_type="staging", target_id=str(staging_id),
                     details={"path": path, "reason": reason[:200]},
                     commit=False)

        # Attach reason as a mana post on the staging realm
        db.execute(conn, """
            INSERT INTO repo_messages
                (repo_id, channel, username, sender_id, content,
                 context_type, context_id)
            VALUES (%s, %s, %s, %s, %s, 'staging', %s)
        """, (r["repo_id"], f"staging-{staging_id}",
              user["username"], user["user_id"],
              reason, str(staging_id)), commit=False)

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Could not create offer: {e}")

    return {"staging_id": staging_id,
            "redirect": f"/repo/{name}/staging/{staging_id}"}


# =========================================================================
# PATCH — multi-file zip upload
# =========================================================================

@app.get("/repo/{name}/tribute/patch", response_class=HTMLResponse)
def tribute_patch_page(name: str, request: Request, conn=Depends(get_db)):
    """Render the patch upload page."""
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    _tribute_visibility_check(conn, r, user)
    return templates.TemplateResponse(request, "tribute_patch.html", {
        "user": user, "repo": r,
        "max_file_mb":  TRIBUTE_MAX_FILE_BYTES  // (1024 * 1024),
        "max_patch_mb": TRIBUTE_MAX_PATCH_BYTES // (1024 * 1024),
        "max_files":    TRIBUTE_MAX_PATCH_FILES,
    })


@app.post("/api/repos/{name}/tribute/patch")
async def tribute_patch_api(name: str, request: Request,
                            conn=Depends(get_db)):
    """
    Accept a ZIP of modified/new files as a staging offer.

    Form fields:
      reason  — why this change should be accepted (required)
      archive — application/zip file upload

    Inside the zip, file paths are relative to repo root. Files present
    in the zip become 'add' or 'modify'. Deletions are not supported in
    patch upload for now — use CLI for that.
    """
    user = get_current_user(request, conn)
    r = repo.get_repo(conn, name)
    if not r:
        raise HTTPException(404)
    _tribute_visibility_check(conn, r, user)

    form = await request.form()
    reason = str(form.get("reason", "")).strip()
    archive = form.get("archive")

    if len(reason) < TRIBUTE_MIN_REASON_CHARS:
        raise HTTPException(400,
            f"Reason must be at least {TRIBUTE_MIN_REASON_CHARS} characters.")
    if archive is None or not hasattr(archive, "read"):
        raise HTTPException(400, "No archive uploaded.")

    zip_bytes = await archive.read()
    if len(zip_bytes) > TRIBUTE_MAX_PATCH_BYTES:
        raise HTTPException(413, "Patch archive too large.")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Uploaded file is not a valid zip.")

    entries = [e for e in zf.infolist() if not e.is_dir()]
    if len(entries) == 0:
        raise HTTPException(400, "Archive is empty.")
    if len(entries) > TRIBUTE_MAX_PATCH_FILES:
        raise HTTPException(400,
            f"Too many files (max {TRIBUTE_MAX_PATCH_FILES}).")

    # Collect (path, bytes) with safety checks
    submitted = []
    for e in entries:
        # Reject path traversal
        p = e.filename.replace("\\", "/")
        if p.startswith("/") or ".." in p.split("/"):
            raise HTTPException(400, f"Unsafe path in archive: {e.filename}")
        if e.file_size > TRIBUTE_MAX_FILE_BYTES:
            raise HTTPException(413,
                f"File '{p}' exceeds size limit.")
        submitted.append((p, zf.read(e)))

    # Compare each submitted file to current canonical; build change list
    objects_dir = os.environ.get(
        "OLYMPUSREPO_OBJECTS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "objects")
    )
    from ..core import objects as obj_store
    from ..core import diff as diff_mod

    branch = r.get("default_branch", "main")
    ref_name = f"refs/heads/{branch}"

    # Get current tree: {path -> blob_hash}
    tree_rows = db.query(conn, """
        SELECT cs.path, cs.change_type, cs.blob_after,
               c.rev
          FROM repo_changesets cs
          JOIN repo_commits c ON c.commit_hash = cs.commit_hash
         WHERE c.repo_id = %s
         ORDER BY c.rev ASC
    """, (r["repo_id"],))

    tree = {}
    for tr in tree_rows:
        if tr["change_type"] in ("add", "modify"):
            tree[tr["path"]] = tr["blob_after"]
        elif tr["change_type"] == "delete":
            tree.pop(tr["path"], None)

    changes = []
    for path, content_bytes in submitted:
        new_hash = obj_store.hash_content(content_bytes)

        # Text check for line counts
        try:
            new_text = content_bytes.decode("utf-8")
            is_binary = False
        except UnicodeDecodeError:
            new_text = ""
            is_binary = True

        if path in tree:
            existing_hash = tree[path]
            if existing_hash == new_hash:
                continue  # identical, skip
            change_type = "modify"
            if not is_binary:
                old_bytes = obj_store.retrieve_blob(existing_hash, objects_dir)
                old_text = old_bytes.decode("utf-8", errors="replace") \
                           if old_bytes else ""
                summary = diff_mod.diff_summary(old_text, new_text)
                la, lr = summary["added"], summary["removed"]
            else:
                la = lr = 0
        else:
            change_type = "add"
            la = 0 if is_binary else len(new_text.splitlines())
            lr = 0

        # Store blob
        obj_store.store_blob(content_bytes, objects_dir)
        changes.append({
            "path": path, "change_type": change_type,
            "blob_hash": new_hash,
            "lines_added": la, "lines_removed": lr,
        })

    if not changes:
        raise HTTPException(400, "No actual changes detected in archive.")

    try:
        branch_name = f"tribute-patch-{user['username']}-{int(time.time())}"
        staging_id = db.query_scalar(conn, """
            INSERT INTO repo_staging
                (repo_id, user_id, branch_name, status)
            VALUES (%s, %s, %s, 'active')
            RETURNING staging_id
        """, (r["repo_id"], user["user_id"], branch_name))

        for c in changes:
            db.execute(conn, """
                INSERT INTO repo_staging_changes
                    (staging_id, path, change_type, blob_hash,
                     lines_added, lines_removed)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (staging_id, c["path"], c["change_type"],
                  c["blob_hash"], c["lines_added"],
                  c["lines_removed"]), commit=False)

        # Notify reviewers
        reviewers = db.query(conn,
            "SELECT user_id FROM repo_users "
            "WHERE role IN ('zeus', 'olympian') AND is_active = TRUE")
        for rev in reviewers:
            db.create_notification(
                conn, user_id=rev["user_id"],
                notif_type="offer_received",
                message=f"Web patch from {user['username']} on {name}: "
                        f"{reason[:80]}",
                link=f"/repo/{name}/staging/{staging_id}",
                repo_id=r["repo_id"],
                commit=False
            )

        db.audit_log(conn, "tribute_patch",
                     user_id=user["user_id"], repo_id=r["repo_id"],
                     target_type="staging", target_id=str(staging_id),
                     details={"files": len(changes),
                              "reason": reason[:200]},
                     commit=False)

        db.execute(conn, """
            INSERT INTO repo_messages
                (repo_id, channel, username, sender_id, content,
                 context_type, context_id)
            VALUES (%s, %s, %s, %s, %s, 'staging', %s)
        """, (r["repo_id"], f"staging-{staging_id}",
              user["username"], user["user_id"],
              reason, str(staging_id)), commit=False)

        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Could not create patch offer: {e}")

    return {"staging_id": staging_id,
            "files": len(changes),
            "redirect": f"/repo/{name}/staging/{staging_id}"}


# =========================================================================
# RUN
# =========================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)