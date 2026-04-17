# =========================================================================
# olympusrepo/core/identity.py
# Instance identity — Ed25519 keypair, signed payloads, relay registration.
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
# MIT License
#
# Every OlympusRepo instance has a permanent cryptographic identity:
#   instance_id  = hex of Ed25519 public key (64 chars)
#   private_key  = base64-encoded raw private key bytes (stored locally)
#
# The identity file lives at .olympusrepo/identity (relative to the
# server working directory, not inside any repo). On first startup,
# if the file doesn't exist, it's generated automatically.
#
# IPs are transient. The keypair is forever.
# =========================================================================

import base64
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

# Location relative to server CWD (same dir as .env)
IDENTITY_FILE = ".olympusrepo/identity"

# =========================================================================
# Load / create
# =========================================================================

def load_or_create(path: str = IDENTITY_FILE) -> dict:
    """
    Return the instance identity dict. Creates it if it doesn't exist.
    Safe to call on every startup — idempotent.

    Returns:
        {
          "instance_id": "64-char hex pubkey",
          "private_key": "base64 raw private key bytes",
          "public_key":  "base64 raw public key bytes",
          "created_at":  "ISO8601Z",
          "human_name":  "hostname or configured name"
        }
    """
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return _generate_and_save(path)


def _generate_and_save(path: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub  = priv.public_key()

    # Raw bytes: private = 32 bytes, public = 32 bytes
    priv_raw = priv.private_bytes_raw()
    pub_raw  = pub.public_bytes_raw()

    identity = {
        "instance_id": pub_raw.hex(),                            # 64 hex chars
        "private_key": base64.b64encode(priv_raw).decode(),
        "public_key":  base64.b64encode(pub_raw).decode(),
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "human_name":  os.environ.get(
                           "OLYMPUSREPO_INSTANCE_NAME",
                           socket.gethostname()
                       ),
    }

    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Write atomically
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(identity, f, indent=2)
    os.replace(tmp, path)

    return identity


# =========================================================================
# Key helpers
# =========================================================================

def _load_private_key(identity: dict):
    """Return an Ed25519PrivateKey from the identity dict."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = base64.b64decode(identity["private_key"])
    return Ed25519PrivateKey.from_private_bytes(raw)


def _load_public_key_from_hex(pubkey_hex: str):
    """Return an Ed25519PublicKey from a 64-char hex string."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = bytes.fromhex(pubkey_hex)
    return Ed25519PublicKey.from_public_bytes(raw)


# =========================================================================
# Signed heartbeat payload
# =========================================================================

def make_heartbeat(identity: dict, port: int,
                   relay_token: str | None = None) -> dict:
    """
    Build a signed registration/heartbeat envelope for relay submission.

    The payload is deterministically serialised (sort_keys=True) before
    signing so the relay can verify with the same serialisation.

    Returns:
        {
          "payload":   { instance_id, ip, port, public_key,
                         human_name, timestamp, relay_token },
          "signature": "hex-encoded Ed25519 signature"
        }
    """
    payload = {
        "instance_id": identity["instance_id"],
        "ip":          _get_outbound_ip(),
        "port":        port,
        "public_key":  identity["instance_id"],   # pubkey IS the instance_id
        "human_name":  identity["human_name"],
        "timestamp":   int(time.time()),
        "relay_token": relay_token,
    }

    payload_bytes = json.dumps(payload, sort_keys=True).encode()
    priv = _load_private_key(identity)
    sig  = priv.sign(payload_bytes)

    return {
        "payload":   payload,
        "signature": sig.hex(),
    }


def verify_heartbeat(envelope: dict,
                     max_age_seconds: int = 120) -> bool:
    """
    Verify a heartbeat envelope from another instance.
    Returns True if the signature is valid and the timestamp is fresh.

    Called by the relay on POST /relay/register.
    """
    try:
        payload   = envelope["payload"]
        sig_bytes = bytes.fromhex(envelope["signature"])

        # Reject stale payloads
        age = int(time.time()) - payload["timestamp"]
        if age > max_age_seconds or age < -10:
            return False

        pubkey_hex    = payload["public_key"]
        payload_bytes = json.dumps(payload, sort_keys=True).encode()

        pub = _load_public_key_from_hex(pubkey_hex)
        pub.verify(sig_bytes, payload_bytes)   # raises on bad sig
        return True

    except Exception:
        return False


# =========================================================================
# Utility
# =========================================================================

def _get_outbound_ip() -> str:
    """
    Best-effort: return the IP this machine uses to reach the internet.
    Does not make an actual connection — just uses routing table probe.
    Falls back to 127.0.0.1 on any error.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def instance_summary(identity: dict) -> str:
    """One-line human-readable summary for logs/CLI output."""
    return (
        f"{identity['human_name']} "
        f"({identity['instance_id'][:12]}...)"
    )