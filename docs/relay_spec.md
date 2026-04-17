# OlympusRelay — Decentralized Instance Discovery Protocol
**Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering — MIT License**

---

## The Problem

Two OlympusRepo instances need to reach each other. Neither has a stable public
IP. Neither controls their NAT. There is no GitHub. There is no Tailscale
coordination server. There is no DNS we trust.

The solution cannot introduce a single point of failure, a single point of
control, or a dependency on any company or service.

---

## Design Principles

1. **No central authority.** Any instance of `olympusrelay` is a peer, not a
   master. The network degrades gracefully as nodes come and go.
2. **Cryptographic identity.** An instance's identity is its public key — not a
   domain, not an IP, not a username. An IP can change. A key pair is forever.
3. **Relay never sees data.** The relay brokers discovery and NAT hole-punch
   handshakes only. Commits, blobs, and offers flow directly instance-to-instance.
4. **Anyone can run a relay.** `pip install olympusrelay && olympusrelay` is the
   full deployment story. University servers, home boxes, VPSes — all equal peers.
5. **Graceful offline.** If every known relay is unreachable, direct IP fallback
   still works. The relay is an enhancement, not a requirement.

---

## Components

```
┌──────────────────────────────────────────────────────────────────────┐
│  Instance A (behind NAT)            Instance B (behind NAT)          │
│  instance_id: pubkey_A              instance_id: pubkey_B            │
│                                                                      │
│  1. Register → relay(s)             1. Register → relay(s)           │
│  2. Query relay for pubkey_B ──────────────────────────────────────► │
│  3. Relay returns {ip, port,                                         │
│     pubkey, token}                                                   │
│  4. Hole-punch via relay ◄──────────────────────────────────────────►│
│  5. Direct HTTP connection ◄────────────────────────────────────────►│
│     (relay no longer involved)                                       │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  Relay Network (gossip mesh)                                         │
│                                                                      │
│  relay1.olympus.community ◄──gossip──► relay2.someuser.net           │
│          ▲                                      ▲                    │
│          │ gossip                               │ gossip              │
│          ▼                                      ▼                    │
│  relay3.university.edu  ◄──gossip──► relay4.yourmachine.ts.net       │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Identity Model

### Key Generation (first run)

On first `olympusrepo` run, if no identity exists:

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_key = Ed25519PrivateKey.generate()
public_key  = private_key.public_key()

# instance_id = hex of raw public key bytes (32 bytes = 64 hex chars)
instance_id = public_key.public_bytes_raw().hex()
```

Stored at `.olympusrepo/identity`:
```json
{
  "instance_id": "a3f7...64hex...b2c1",
  "private_key": "base64-encoded-raw-private-key",
  "public_key":  "base64-encoded-raw-public-key",
  "created_at":  "2026-04-17T07:31:00Z",
  "human_name":  "Sean's Olympus"
}
```

**The `instance_id` is the only address that matters.** IPs are transient metadata.

### Signed Heartbeats

Every registration and heartbeat is signed:

```python
import time, json

payload = {
  "instance_id": instance_id,
  "ip":          "203.0.113.42",
  "port":        8000,
  "public_key":  pubkey_hex,
  "human_name":  "Sean's Olympus",
  "timestamp":   int(time.time()),   # relay rejects if > 120s old
  "relay_token": None,               # filled in after first registration
}

payload_bytes = json.dumps(payload, sort_keys=True).encode()
signature = private_key.sign(payload_bytes)
envelope = {
  "payload":   payload,
  "signature": signature.hex()
}
```

The relay verifies the signature with the embedded `public_key` before storing.
Replayed or forged registrations are rejected. A rogue relay cannot impersonate
an instance it does not hold the private key for.

---

## OlympusRelay Service

### What It Stores (in memory + optional SQLite)

```python
# In-memory registry (SQLite mirror for persistence across restarts)
registry: dict[str, InstanceRecord] = {}

@dataclass
class InstanceRecord:
    instance_id: str
    ip:          str
    port:        int
    public_key:  str          # hex ed25519 pubkey
    human_name:  str
    last_seen:   float        # unix timestamp
    relay_token: str          # 32-byte random token, issued on first register
    source:      str          # "direct" | "gossip"
    via_relay:   str | None   # relay that gossiped this to us
```

Entries expire after **10 minutes** of no heartbeat. Expired entries are kept
for 1 hour as tombstones (to prevent gossip resurrection of dead nodes).

### API Endpoints

All endpoints are unauthenticated HTTP. The relay carries no secrets — the
cryptographic signatures on payloads are the authentication.

---

#### `POST /relay/register`

Register or refresh an instance.

**Request:**
```json
{
  "payload": {
    "instance_id": "a3f7...b2c1",
    "ip":          "203.0.113.42",
    "port":        8000,
    "public_key":  "a3f7...b2c1",
    "human_name":  "Sean's Olympus",
    "timestamp":   1745000000,
    "relay_token": null
  },
  "signature": "deadbeef...hex"
}
```

**Response (first registration):**
```json
{
  "status":      "registered",
  "relay_token": "f4a2...32hex...9c1b",
  "relay_id":    "relay1.olympus.community",
  "peers":       ["relay2.someuser.net", "relay3.university.edu"]
}
```

`relay_token` is used for hole-punch coordination. The instance stores it and
includes it in subsequent heartbeats. The relay issues the same token on every
registration for the same `instance_id` (derived from the public key, not random
each time — use `HMAC(relay_secret, instance_id)`).

**Response (refresh):**
```json
{
  "status":      "refreshed",
  "relay_token": "f4a2...32hex...9c1b"
}
```

**Error cases:**
- `400` — timestamp too old (> 120 seconds)
- `400` — signature verification failed
- `429` — rate limited (max 1 registration per 30 seconds per instance_id)

---

#### `GET /relay/find/{instance_id}`

Look up an instance by its identity.

**Response (found):**
```json
{
  "instance_id": "a3f7...b2c1",
  "ip":          "203.0.113.42",
  "port":        8000,
  "public_key":  "a3f7...b2c1",
  "human_name":  "Sean's Olympus",
  "last_seen":   1745000000,
  "stale":       false
}
```

**Response (not found):**
```json
{
  "status": "not_found",
  "searched_peers": ["relay2.someuser.net"]
}
```

If the relay doesn't have the record, it **asks its known peers** before
responding `not_found`. This is a single fan-out query, not recursive gossip —
depth 1 only to prevent query amplification.

---

#### `POST /relay/punch`

Request hole-punch coordination between two instances.

This is the mechanism that allows two NAT-ed machines to establish a direct
connection. The relay acts as a signaling server only — it tells both sides to
send UDP packets to each other simultaneously, which opens NAT pinholes.

**Request (from Instance A, wanting to reach Instance B):**
```json
{
  "from_instance": "a3f7...b2c1",
  "to_instance":   "e9d2...b7a4",
  "relay_token":   "f4a2...32hex...9c1b",
  "my_port":       8000
}
```

**Flow:**
1. Relay receives punch request from A
2. Relay looks up B's current IP:port
3. Relay sends a `punch_notify` webhook to B: `POST http://B_ip:B_port/relay/punch_incoming`
4. Both A and B simultaneously send UDP keepalives to each other's IP:port
5. NAT pinholes open on both sides
6. A upgrades to direct HTTP connection to B

**Response:**
```json
{
  "status":     "coordinating",
  "target_ip":  "198.51.100.77",
  "target_port": 8001,
  "punch_id":   "uuid4"
}
```

---

#### `GET /relay/punch_incoming` (on OlympusRepo instance, not relay)

OlympusRepo instances expose this endpoint. The relay calls it to notify an
instance that someone wants to punch through:

```json
{
  "from_instance": "a3f7...b2c1",
  "from_ip":       "203.0.113.42",
  "from_port":     8000,
  "punch_id":      "uuid4"
}
```

The receiving instance then sends UDP packets to `from_ip:from_port` to open
its own NAT pinhole, completing the handshake.

---

#### `GET /relay/list`

Returns all currently registered (non-expired) instances. Public.

```json
{
  "instances": [
    {
      "instance_id": "a3f7...b2c1",
      "human_name":  "Sean's Olympus",
      "last_seen":   1745000000,
      "stale":       false
    }
  ],
  "count": 1,
  "relay_id": "relay1.olympus.community"
}
```

Note: IPs and ports are **not** returned in the list endpoint — only via
`/relay/find/{instance_id}` which requires knowing the specific ID you're
looking for. This prevents the relay from becoming a scanner/harvester tool.

---

#### `POST /relay/gossip`

Relay-to-relay sync. Called automatically on a 60-second interval between
known peer relays.

**Request:**
```json
{
  "from_relay":  "relay2.someuser.net",
  "instances":   [
    {
      "instance_id": "b8c3...a1f2",
      "ip":          "198.51.100.77",
      "port":        8001,
      "public_key":  "b8c3...a1f2",
      "human_name":  "Athens",
      "last_seen":   1745000000
    }
  ]
}
```

The receiving relay merges incoming records, keeping whichever `last_seen` is
more recent. It does NOT re-gossip received records (depth-1 gossip only — no
gossip storms).

---

#### `GET /relay/peers`

Returns the relay's known peer relays.

```json
{
  "peers": [
    {"url": "relay2.someuser.net", "last_contact": 1745000000, "healthy": true},
    {"url": "relay3.university.edu", "last_contact": 1744990000, "healthy": false}
  ]
}
```

---

#### `GET /relay/health`

Simple liveness check. Returns 200 with:
```json
{
  "status":      "ok",
  "relay_id":    "relay1.olympus.community",
  "version":     "0.1.0",
  "instances":   42,
  "uptime_s":    86400
}
```

---

## Gossip Protocol

### Relay-to-Relay Mesh

```
Startup:
  1. Load peer list from config (bootstrap list)
  2. Call GET /relay/peers on each bootstrap peer
  3. Merge returned peers into own peer list
  4. Begin gossip loop

Every 60 seconds:
  for each known peer relay:
    try:
      POST /relay/gossip  ← send our current non-stale registry
      GET /relay/peers    ← learn about new peers they know
    except timeout/error:
      mark peer unhealthy
      if unhealthy for > 30 minutes: remove from active peers

Peer discovery (transitive):
  When we learn of a new relay via /relay/peers,
  add it to our peer list and begin gossiping with it.
  This is how the mesh self-heals and grows organically.
```

### Why Depth-1 Only

Full epidemic gossip (each relay re-gossips what it receives) causes O(n²)
message amplification on a large network. Since we only care about live
instances and relays expire stale records at 10 minutes, a single round of
gossip is sufficient — every relay sees every live instance within one 60-second
cycle without any relay needing to forward gossip it received.

---

## OlympusRepo Integration

### Changes to `cli.py` / `app.py`

#### On first run — identity generation
```python
# olympusrepo/core/identity.py  (new file)

IDENTITY_PATH = ".olympusrepo/identity"

def load_or_create() -> dict:
    if os.path.exists(IDENTITY_PATH):
        return json.load(open(IDENTITY_PATH))
    return _generate_and_save()

def _generate_and_save() -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub  = priv.public_key()
    identity = {
        "instance_id": pub.public_bytes_raw().hex(),
        "private_key": base64.b64encode(priv.private_bytes_raw()).decode(),
        "public_key":  base64.b64encode(pub.public_bytes_raw()).decode(),
        "created_at":  datetime.utcnow().isoformat() + "Z",
        "human_name":  socket.gethostname(),
    }
    os.makedirs(".olympusrepo", exist_ok=True)
    json.dump(identity, open(IDENTITY_PATH, "w"), indent=2)
    return identity
```

#### Heartbeat background thread (in `app.py`)
```python
# Starts on server startup, runs every 5 minutes
def _relay_heartbeat_loop():
    identity = identity_mod.load_or_create()
    relays   = config.get_relay_list()   # from .env / DB
    while True:
        for relay_url in relays:
            try:
                _register_with_relay(relay_url, identity)
            except Exception:
                pass
        time.sleep(300)

threading.Thread(target=_relay_heartbeat_loop, daemon=True).start()
```

#### Relay-resolved clone
```bash
# Instead of:
olympusrepo clone http://192.168.x.x:8000/repo/myproject

# With relay:
olympusrepo clone olympus://a3f7b2c1.../myproject
```

The `olympus://` scheme triggers relay resolution:
1. Query known relays for `instance_id = a3f7b2c1...`
2. Verify returned IP signs correctly against `public_key`
3. Substitute resolved `http://ip:port` and proceed as normal clone

---

## `.env` / Config Additions

```bash
# Relay settings (added to .env by setup.sh)

# This instance's human-readable name (shown in relay /list)
OLYMPUSREPO_INSTANCE_NAME="Sean's Olympus"

# Comma-separated relay URLs to register with and query
OLYMPUSREPO_RELAYS="https://relay1.olympus.community,https://relay2.someuser.net"

# Set to 0 to disable relay registration entirely (fully private mode)
OLYMPUSREPO_RELAY_ENABLED=1

# If running a relay yourself
OLYMPUSREPO_RELAY_PEERS="https://relay1.olympus.community"
```

---

## Bootstrap Relay List

Shipped hardcoded in `olympusrepo/relay_bootstrap.py`:

```python
BOOTSTRAP_RELAYS = [
    "https://relay1.olympus.community",   # project-run, best-effort
    "https://relay2.olympus.community",   # project-run, best-effort
]
```

These are **best-effort community relays** — not guaranteed uptime, not a
business, just servers running `olympusrelay`. The project documentation
explicitly encourages anyone with a VPS or home server to run their own relay
and add it to their `OLYMPUSREPO_RELAYS`.

If all bootstrap relays are unreachable, the system falls back to direct IP
entry in `repo_remotes` — the relay is an enhancement, never a hard dependency.

---

## `olympusrelay` — The Standalone Relay Service

Ships as a separate installable in the same repo under `relay/`.

```
relay/
├── olympusrelay/
│   ├── __init__.py
│   ├── app.py        # FastAPI routes (all endpoints above)
│   ├── registry.py   # In-memory store + SQLite mirror
│   ├── gossip.py     # Background gossip loop
│   └── config.py     # Env-based config
├── setup.py
└── README.md
```

**Install and run:**
```bash
pip install olympusrelay

# Defaults: port 9000, no peers (bootstrap-only mode)
olympusrelay

# With peer relays:
olympusrelay --port 9000 --peers relay1.olympus.community,relay2.someuser.net

# Or via env:
OLYMPUSRELAY_PORT=9000 \
OLYMPUSRELAY_PEERS="relay1.olympus.community" \
olympusrelay
```

**Docker (one-liner for VPS deployment):**
```bash
docker run -p 9000:9000 \
  -e OLYMPUSRELAY_PEERS="relay1.olympus.community" \
  ghcr.io/ailang-author/olympusrelay:latest
```

`olympusrelay` has **zero database dependencies** — it uses SQLite for
persistence across restarts and holds the live registry in memory. The whole
service is ~300 lines of Python.

---

## Security Model

### What the relay knows
- Your `instance_id` (public key — not secret by design)
- Your current IP and port
- Your human-readable instance name
- When you last checked in

### What the relay cannot do
- Read your commits, blobs, or offers (never proxied through relay)
- Impersonate your instance (lacks your private key)
- Forge registrations from other instances (signature verification)
- Inject stale/old registrations (timestamp window check)
- Track which instances are talking to which (hole-punch is stateless —
  relay notifies B then forgets the punch session)

### What a malicious relay can do
- Return a wrong IP for a `find` query (send you to the wrong instance)
- **Mitigation:** OlympusRepo verifies the returned public key by sending a
  signed challenge to the resolved IP before trusting it. If the response
  doesn't verify against the known `instance_id`, the connection is rejected.

### Signed challenge on connect
```python
# Before accepting a resolved address:
challenge = secrets.token_hex(32)
response  = requests.post(f"http://{ip}:{port}/relay/challenge",
                           json={"challenge": challenge})
signature = bytes.fromhex(response.json()["signature"])
pubkey    = Ed25519PublicKey.from_public_bytes(bytes.fromhex(instance_id))
pubkey.verify(signature, challenge.encode())  # raises if invalid
# Only now proceed with clone/pull/offer
```

---

## Phase Rollout

### v0.5 — Relay Alpha
- `olympusrelay` standalone service
- `POST /relay/register`, `GET /relay/find`, `GET /relay/list`, `GET /relay/health`
- Gossip between relays
- Heartbeat thread in `olympusrepo` app startup
- `olympus://` scheme resolution in `cmd_clone`
- Two community bootstrap relays running

### v0.6 — Hole Punch
- `POST /relay/punch` + `GET /relay/punch_incoming` on instances
- UDP NAT traversal
- Direct connection after punch succeeds
- Fallback to relay-proxied HTTP if punch fails (for symmetric NAT)

### v0.7 — Relay Hardening
- Rate limiting on all relay endpoints
- Tombstone tracking (prevent gossip resurrection)
- Relay health dashboard (web UI on the relay itself)
- `OLYMPUSREPO_RELAY_ENABLED=0` fully private mode tested
- Docker image published

---

## Why Not [X]

**Why not libp2p?**
Full p2p networking stack. Complex, large dependency, designed for blockchain
use cases. OlympusRepo needs discovery and hole-punching, not a DHT content
network. This design is 300 lines vs 300,000.

**Why not STUN/TURN (WebRTC)?**
STUN is actually the inspiration for the hole-punch mechanism. But WebRTC's
signaling infrastructure assumes a browser context. The relay IS the signaling
server — this is STUN without the WebRTC weight.

**Why not Tor?**
Tor routes ALL traffic through the onion network. Latency makes blob transfer
painful. Good for anonymity-required use cases, bad as the default. Available
as a setup.sh option but not the relay's job.

**Why not just use DNS?**
DNS requires domain ownership, propagation delay, and a registrar — all
centralized dependencies. The relay resolves by cryptographic identity, not
by name. You can have a human name AND a cryptographic identity; the human
name is just a label, not the address.
