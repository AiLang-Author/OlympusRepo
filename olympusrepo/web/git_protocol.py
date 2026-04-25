"""
olympusrepo/web/git_protocol.py
Smart HTTP git protocol endpoints.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License

Routes (mount under app.include_router(router)):
  GET  /{repo_name}.git/info/refs?service=git-upload-pack
  GET  /{repo_name}.git/info/refs?service=git-receive-pack
  POST /{repo_name}.git/git-upload-pack
  POST /{repo_name}.git/git-receive-pack

The handlers are byte pipes into the git binaries running against the
gateway bare repo. We add auth, access log, and post-push reingest.
"""

import asyncio
import base64
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Request, Response, HTTPException, Depends
from starlette.responses import StreamingResponse

from ..core import gateway, pats, import_git
from ..core import db as db_mod


router = APIRouter()

# Matches the subset of repo names we allow: same rules as the rest of
# the app's naming. Tightening this makes path-traversal impossible
# before we ever touch the filesystem.
_REPO_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$')

# Read this many bytes at a time when shovelling between client and
# subprocess. 64 KiB is what the git http-backend uses.
_PIPE_CHUNK = 64 * 1024

# Hard cap on receive-pack request bodies, defensive against zip-bomb-
# style pushes. 10 GiB default; adjust via env if a user legitimately
# needs more.
_MAX_RECEIVE_BYTES = int(os.environ.get(
    "OLYMPUSREPO_MAX_RECEIVE_BYTES", str(10 * 1024 * 1024 * 1024)
))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class AuthContext:
    __slots__ = ("user_id", "via", "scopes")

    def __init__(self, user_id: Optional[int], via: str, scopes: list[str]):
        self.user_id = user_id
        self.via = via  # 'anon', 'pat', 'password'
        self.scopes = scopes


def _parse_basic_auth(header: str) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    user, _, password = decoded.partition(":")
    return user, password


async def authenticate(request: Request) -> AuthContext:
    """
    Extract and validate Basic auth. No creds = anonymous (scope ['public']).
    Password creds = verify against repo_users.
    PAT creds = verify against repo_pats (scopes come from the PAT row).
    """
    header = request.headers.get("authorization", "")
    parsed = _parse_basic_auth(header)
    if parsed is None:
        return AuthContext(user_id=None, via="anon", scopes=["public"])

    username, password = parsed
    conn = db_mod.connect()

    # PAT path: password field starts with the olyp_ prefix.
    if password.startswith(pats.TOKEN_PREFIX):
        result = pats.verify_pat(conn, password)
        if not result:
            raise HTTPException(status_code=401, detail="invalid token")
        return AuthContext(
            user_id=result["user_id"], via="pat", scopes=result["scopes"],
        )

    # Password path.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT repo_verify_password(%s, %s)", (username, password),
        )
        user_id = cur.fetchone()[0]
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return AuthContext(
        user_id=user_id, via="password",
        scopes=["git:read", "git:write", "api:read", "api:write"],
    )


def _challenge() -> Response:
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="OlympusRepo"'},
    )


# ---------------------------------------------------------------------------
# Repo resolution + ACL
# ---------------------------------------------------------------------------
def _resolve_repo(conn, repo_name: str) -> dict:
    if not _REPO_NAME_RE.match(repo_name):
        raise HTTPException(status_code=404, detail="repo not found")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT repo_id, name, visibility, owner_id
            FROM repo_repositories
            WHERE name = %s
        """, (repo_name,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="repo not found")
    return dict(zip(("repo_id", "name", "visibility", "owner_id"), row))


def _check_read(conn, repo: dict, auth: AuthContext) -> None:
    if repo["visibility"] == "public":
        return
    if auth.user_id is None:
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OlympusRepo"'},
            detail="authentication required",
        )
    if "git:read" not in auth.scopes and "public" not in auth.scopes:
        raise HTTPException(status_code=403, detail="missing git:read scope")
    if repo["owner_id"] == auth.user_id:
        return
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM repo_access
            WHERE repo_id = %s AND user_id = %s
        """, (repo["repo_id"], auth.user_id))
        if cur.fetchone():
            return
    raise HTTPException(status_code=404, detail="repo not found")


def _check_write(conn, repo: dict, auth: AuthContext) -> None:
    if auth.user_id is None:
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OlympusRepo"'},
            detail="authentication required",
        )
    if "git:write" not in auth.scopes:
        raise HTTPException(status_code=403, detail="missing git:write scope")
    if repo["owner_id"] == auth.user_id:
        return
    with conn.cursor() as cur:
        cur.execute("""
            SELECT access_level FROM repo_access
            WHERE repo_id = %s AND user_id = %s
        """, (repo["repo_id"], auth.user_id))
        row = cur.fetchone()
    if not row or row[0] not in ("write", "admin"):
        raise HTTPException(status_code=403, detail="write access denied")


# ---------------------------------------------------------------------------
# pkt-line framing
# ---------------------------------------------------------------------------
def _pkt_line(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


_FLUSH_PKT = b"0000"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/{repo_name}.git/info/refs")
async def info_refs(
    repo_name: str, request: Request,
    auth: AuthContext = Depends(authenticate),
):
    service = request.query_params.get("service", "")
    if service not in ("git-upload-pack", "git-receive-pack"):
        raise HTTPException(status_code=400, detail="unsupported service")

    conn = db_mod.connect()
    repo = _resolve_repo(conn, repo_name)

    if service == "git-upload-pack":
        _check_read(conn, repo, auth)
    else:
        _check_write(conn, repo, auth)

    # Keep the gateway up to date before advertising refs.
    from ..core import repo as repo_mod
    objects_dir = os.environ.get("OLYMPUSREPO_OBJECTS_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "objects"))
    gateway.ensure_gateway_synced(
        conn, repo_id=repo["repo_id"], objects_dir=objects_dir,
    )
    gw_path = gateway.gateway_path(repo["repo_id"])

    # Run git service --stateless-rpc --advertise-refs and prepend the
    # service header per the smart HTTP spec.
    proc = await asyncio.create_subprocess_exec(
        import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
        service.removeprefix("git-"), "--stateless-rpc",
        "--advertise-refs", gw_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=import_git.GIT_ENV,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"{service} failed: {err.decode('utf-8', errors='replace')[:200]}",
        )

    header = _pkt_line(f"# service={service}\n".encode("ascii")) + _FLUSH_PKT
    body = header + out

    _log_protocol(
        conn, repo["repo_id"], auth.user_id,
        f"info-refs-{'upload' if service == 'git-upload-pack' else 'receive'}",
        bytes_out=len(body), status=200,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )

    return Response(
        content=body,
        media_type=f"application/x-{service}-advertisement",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{repo_name}.git/git-upload-pack")
async def upload_pack(
    repo_name: str, request: Request,
    auth: AuthContext = Depends(authenticate),
):
    conn = db_mod.connect()
    repo = _resolve_repo(conn, repo_name)
    _check_read(conn, repo, auth)
    gw_path = gateway.gateway_path(repo["repo_id"])

    return await _run_pack_service(
        service="upload-pack",
        gateway_path=gw_path,
        request=request,
        repo_id=repo["repo_id"],
        auth=auth,
        conn=conn,
    )


@router.post("/{repo_name}.git/git-receive-pack")
async def receive_pack(
    repo_name: str, request: Request,
    auth: AuthContext = Depends(authenticate),
):
    conn = db_mod.connect()
    repo = _resolve_repo(conn, repo_name)
    _check_write(conn, repo, auth)
    gw_path = gateway.gateway_path(repo["repo_id"])

    # We need to know what refs changed so we can reingest them into
    # Olympus after git-receive-pack finishes. The client's request
    # body starts with pkt-line "old new ref" updates, then the pack.
    # Rather than parse the stream, read the gateway refs before and
    # after.
    refs_before = _read_gateway_refs(gw_path)

    response = await _run_pack_service(
        service="receive-pack",
        gateway_path=gw_path,
        request=request,
        repo_id=repo["repo_id"],
        auth=auth,
        conn=conn,
    )

    refs_after = _read_gateway_refs(gw_path)
    changes: list[tuple[str, str, str]] = []
    all_refs = set(refs_before) | set(refs_after)
    for ref in all_refs:
        b = refs_before.get(ref, "0" * 40)
        a = refs_after.get(ref, "0" * 40)
        if b != a:
            changes.append((b, a, ref))

    if changes:
        from ..core import repo as repo_mod
        objects_dir = os.environ.get("OLYMPUSREPO_OBJECTS_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "objects"))
        try:
            gateway.reingest_from_gateway(
                conn, repo_id=repo["repo_id"],
                ref_updates=changes,
                objects_dir=objects_dir,
                importer_user_id=auth.user_id,
            )
        except Exception as e:
            # The client's push succeeded at the git level but we
            # failed to mirror it into Olympus. Log loudly; the
            # gateway is still consistent and the next ensure_sync
            # will pick it up.
            _log_protocol(
                conn, repo["repo_id"], auth.user_id,
                "receive-pack",
                error=f"reingest failed: {e}",
            )

    return response


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------
async def _run_pack_service(
    *, service: str, gateway_path: str, request: Request,
    repo_id: int, auth: AuthContext, conn,
) -> Response:
    """
    Run git-upload-pack / git-receive-pack against the gateway repo and
    proxy its stdout back as the HTTP response body.

    Non-streaming: we read the full request body up-front and feed it
    to the subprocess via communicate(). The previous streaming
    implementation deadlocked because request.stream() inside an
    asyncio.create_task() never yielded — looked like a Starlette/
    asyncio scheduling interaction with StreamingResponse. Smart-HTTP
    request bodies for typical clones are KB-range, so the simpler
    buffered approach is fine.
    """
    started = time.monotonic()

    # Read the client's full body (want/have negotiation lines, then
    # for receive-pack the pack itself). _MAX_RECEIVE_BYTES caps it so
    # a hostile push can't OOM us.
    body = await request.body()
    if len(body) > _MAX_RECEIVE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="push exceeds maximum size",
        )

    proc = await asyncio.create_subprocess_exec(
        import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
        service, "--stateless-rpc", gateway_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=import_git.GIT_ENV,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=body),
            timeout=import_git.GIT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail=f"{service} timed out")

    duration_ms = int((time.monotonic() - started) * 1000)
    rc = proc.returncode

    _log_protocol(
        conn, repo_id, auth.user_id, service,
        bytes_in=len(body), bytes_out=len(stdout),
        status=200 if rc == 0 else 500,
        duration_ms=duration_ms,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )

    if rc != 0:
        # Surface git's stderr (truncated) so debugging doesn't require
        # crawling server logs. status 500 is the right tier here — the
        # client request was structurally valid; the backend failed.
        err_text = stderr.decode("utf-8", errors="replace")[:600] if stderr else ""
        raise HTTPException(
            status_code=500,
            detail=f"{service} failed (rc {rc}): {err_text.strip()}",
        )

    return Response(
        content=stdout,
        media_type=f"application/x-git-{service}-result",
        headers={"Cache-Control": "no-cache"},
    )


def _read_gateway_refs(gateway_path: str) -> dict[str, str]:
    import subprocess
    out = subprocess.run(
        [import_git.GIT_BIN, *import_git.GIT_SAFE_ARGS,
         "-C", gateway_path, "for-each-ref",
         "--format=%(refname) %(objectname)"],
        capture_output=True, text=True,
        env=import_git.GIT_ENV,
        timeout=import_git.GIT_TIMEOUT_SECONDS,
    )
    refs = {}
    for line in out.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


def _log_protocol(
    conn, repo_id: int, user_id: Optional[int],
    operation: str, *,
    bytes_in: int = 0, bytes_out: int = 0,
    status: int | None = None, error: str | None = None,
    duration_ms: int | None = None,
    user_agent: str | None = None, ip: str | None = None,
) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO repo_git_protocol_log
                    (repo_id, user_id, operation,
                     bytes_in, bytes_out, status_code,
                     error_message, user_agent, ip_address, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (repo_id, user_id, operation,
                  bytes_in, bytes_out, status,
                  error, user_agent, ip, duration_ms))
    except Exception:
        # Never let logging fail the request.
        pass
