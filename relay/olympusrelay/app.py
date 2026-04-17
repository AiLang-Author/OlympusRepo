# relay/olympusrelay/app.py
# OlympusRelay — decentralized instance discovery service.
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering — MIT
#
# Run:  olympusrelay
#       olympusrelay --port 9000 --peers relay1.olympus.community

import argparse
import sys
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config
from . import gossip as gossip_mod
from .registry import registry

# ── Import identity verifier from the main olympusrepo package if available,
#    otherwise provide a local stub for standalone relay deployments. ────────
try:
    from olympusrepo.core.identity import verify_heartbeat
except ImportError:
    # Standalone relay — inline the verifier (no olympusrepo dep needed)
    import base64, json as _json

    def verify_heartbeat(envelope: dict, max_age_seconds: int = 120) -> bool:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            payload   = envelope["payload"]
            sig_bytes = bytes.fromhex(envelope["signature"])
            age = int(time.time()) - payload["timestamp"]
            if age > max_age_seconds or age < -10:
                return False
            pub_bytes = bytes.fromhex(payload["public_key"])
            pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
            payload_bytes = _json.dumps(payload, sort_keys=True).encode()
            pub.verify(sig_bytes, payload_bytes)
            return True
        except Exception:
            return False


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    relay_id = config.RELAY_ID or "olympusrelay"
    print(f"  OlympusRelay starting — id: {relay_id}")
    print(f"  Port:    {config.PORT}")
    print(f"  Peers:   {config.PEERS or '(none — bootstrap mode)'}")
    print(f"  TTL:     {config.TTL_SECONDS}s")
    gossip_mod.start()
    yield


app = FastAPI(title="OlympusRelay", lifespan=lifespan)


# ── POST /relay/register ─────────────────────────────────────────────────────

@app.post("/relay/register")
async def register(request: Request):
    envelope = await request.json()

    if not verify_heartbeat(envelope, config.MAX_AGE_PAYLOAD):
        raise HTTPException(400, "Invalid or stale signature.")

    payload = envelope["payload"]
    iid     = payload.get("instance_id", "")
    if not iid or len(iid) != 64:
        raise HTTPException(400, "Invalid instance_id.")

    existing = registry.find(iid)
    token    = registry.register(payload, source="direct")
    status   = "refreshed" if existing else "registered"

    return {
        "status":      status,
        "relay_token": token,
        "relay_id":    config.RELAY_ID or "olympusrelay",
        "peers":       config.PEERS,
    }


# ── GET /relay/find/{instance_id} ────────────────────────────────────────────

@app.get("/relay/find/{instance_id}")
async def find(instance_id: str):
    rec = registry.find(instance_id)

    if rec:
        return {
            "instance_id": rec.instance_id,
            "ip":          rec.ip,
            "port":        rec.port,
            "public_key":  rec.public_key,
            "human_name":  rec.human_name,
            "last_seen":   rec.last_seen,
            "stale":       False,
        }

    # Not found locally — fan out to peers (depth-1 only)
    searched = []
    for peer in config.PEERS:
        url = peer.rstrip("/") + f"/relay/find/{instance_id}"
        searched.append(peer)
        try:
            r = httpx.get(url, timeout=3)
            if r.status_code == 200:
                data = r.json()
                # Merge into our registry so future queries hit local cache
                registry.register(data, source="gossip", via_relay=peer)
                return data
        except Exception:
            pass

    return JSONResponse(
        status_code=404,
        content={"status": "not_found", "searched_peers": searched}
    )


# ── POST /relay/punch ────────────────────────────────────────────────────────

@app.post("/relay/punch")
async def punch(request: Request):
    body = await request.json()
    to_id = body.get("to_instance")
    if not to_id:
        raise HTTPException(400, "to_instance required.")

    target = registry.find(to_id)
    if not target:
        raise HTTPException(404, "Target instance not found.")

    import uuid
    punch_id = str(uuid.uuid4())

    # Notify target instance
    notify_url = f"http://{target.ip}:{target.port}/relay/punch_incoming"
    try:
        httpx.post(notify_url, json={
            "from_instance": body.get("from_instance"),
            "from_ip":       body.get("my_ip", ""),
            "from_port":     body.get("my_port", 0),
            "punch_id":      punch_id,
        }, timeout=3)
    except Exception:
        pass  # target may not be reachable yet — that's the point of hole-punch

    return {
        "status":      "coordinating",
        "target_ip":   target.ip,
        "target_port": target.port,
        "punch_id":    punch_id,
    }


# ── GET /relay/list ──────────────────────────────────────────────────────────

@app.get("/relay/list")
async def list_instances():
    live = registry.list_live()
    return {
        "instances": [
            {
                "instance_id": r.instance_id,
                "human_name":  r.human_name,
                "last_seen":   r.last_seen,
                "stale":       False,
            }
            for r in live
        ],
        "count":    len(live),
        "relay_id": config.RELAY_ID or "olympusrelay",
    }


# ── POST /relay/gossip ───────────────────────────────────────────────────────

@app.post("/relay/gossip")
async def gossip(request: Request):
    body       = await request.json()
    from_relay = body.get("from_relay", "unknown")
    records    = body.get("instances", [])

    registry.merge_gossip(records, via_relay=from_relay)

    # Return our own records so the peer can merge them too (single exchange)
    return {
        "status":    "merged",
        "instances": registry.list_for_gossip(),
    }


# ── GET /relay/peers ─────────────────────────────────────────────────────────

@app.get("/relay/peers")
async def peers():
    return {"peers": config.PEERS, "relay_id": config.RELAY_ID or "olympusrelay"}


# ── GET /relay/health ────────────────────────────────────────────────────────

@app.get("/relay/health")
async def health():
    return {"status": "ok", "instances": len(registry.list_live())}


# ── CLI entrypoint ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OlympusRelay — instance discovery relay")
    parser.add_argument("--port",   type=int, default=config.PORT)
    parser.add_argument("--peers",  help="Comma-separated peer relay URLs")
    parser.add_argument("--id",     help="Human name for this relay")
    parser.add_argument("--db",     help="SQLite path (default: relay.db)")
    parser.add_argument("--secret", help="HMAC secret for token generation")
    args = parser.parse_args()

    # Override config from CLI args
    if args.peers:
        config.PEERS[:] = [p.strip() for p in args.peers.split(",") if p.strip()]
    if args.id:
        config.RELAY_ID = args.id
    if args.db:
        config.DB_PATH = args.db
    if args.secret:
        config.SECRET = args.secret

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
