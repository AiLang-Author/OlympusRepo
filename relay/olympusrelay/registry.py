# relay/olympusrelay/registry.py
# In-memory instance registry with SQLite mirror for persistence.
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering — MIT

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import config


@dataclass
class InstanceRecord:
    instance_id: str
    ip:          str
    port:        int
    public_key:  str
    human_name:  str
    last_seen:   float
    relay_token: str
    source:      str          # "direct" | "gossip"
    via_relay:   Optional[str] = None
    envelope:    Optional[str] = None   # original signed heartbeat JSON


class Registry:
    """
    Thread-safe in-memory registry of live OlympusRepo instances.
    SQLite is a write-through mirror — used only for restart recovery.
    All reads come from memory.
    """

    def __init__(self):
        self._lock     = threading.Lock()
        self._live: dict[str, InstanceRecord] = {}
        self._db: Optional[sqlite3.Connection] = None
        self._init_db()
        self._load_from_db()

    # ── SQLite setup ──────────────────────────────────────────────────────

    def _init_db(self):
        self._db = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS instances (
                instance_id TEXT PRIMARY KEY,
                ip          TEXT NOT NULL,
                port        INTEGER NOT NULL,
                public_key  TEXT NOT NULL,
                human_name  TEXT NOT NULL,
                last_seen   REAL NOT NULL,
                relay_token TEXT NOT NULL,
                source      TEXT NOT NULL,
                via_relay   TEXT,
                envelope    TEXT
            )
        """)
        # Migrate older DBs that predate the envelope column.
        cols = {row["name"] for row in
                self._db.execute("PRAGMA table_info(instances)").fetchall()}
        if "envelope" not in cols:
            self._db.execute("ALTER TABLE instances ADD COLUMN envelope TEXT")
        self._db.commit()

    def _load_from_db(self):
        """Load non-expired records from SQLite on startup."""
        cutoff = time.time() - config.TTL_SECONDS
        rows = self._db.execute(
            "SELECT * FROM instances WHERE last_seen > ?", (cutoff,)
        ).fetchall()
        for row in rows:
            r = InstanceRecord(**dict(row))
            self._live[r.instance_id] = r

    def _persist(self, r: InstanceRecord):
        self._db.execute("""
            INSERT INTO instances
                (instance_id, ip, port, public_key, human_name,
                 last_seen, relay_token, source, via_relay, envelope)
            VALUES
                (:instance_id,:ip,:port,:public_key,:human_name,
                 :last_seen,:relay_token,:source,:via_relay,:envelope)
            ON CONFLICT(instance_id) DO UPDATE SET
                ip          = excluded.ip,
                port        = excluded.port,
                last_seen   = excluded.last_seen,
                relay_token = excluded.relay_token,
                source      = excluded.source,
                via_relay   = excluded.via_relay,
                envelope    = excluded.envelope
        """, asdict(r))
        self._db.commit()

    # ── Token generation ─────────────────────────────────────────────────

    def _make_token(self, instance_id: str) -> str:
        """
        Deterministic per-instance token derived from relay secret.
        Same token is always issued for the same instance_id so
        re-registrations get the same token without storing state.
        Config.ensure_secret() guarantees SECRET is set before first call.
        """
        if not config.SECRET:
            raise RuntimeError(
                "OLYMPUSRELAY_SECRET is not configured. "
                "Refusing to issue tokens with a default secret."
            )
        key = config.SECRET.encode()
        return hmac.new(key, instance_id.encode(), hashlib.sha256).hexdigest()

    # ── Public API ───────────────────────────────────────────────────────

    def register(self, payload: dict, source: str = "direct",
                 via_relay: str = None,
                 envelope: Optional[dict] = None) -> str:
        """
        Register or refresh an instance. Returns the relay_token.
        ``envelope`` is the verified signed envelope the payload came from —
        stored verbatim so we can re-gossip without re-signing.
        """
        iid   = payload["instance_id"]
        token = self._make_token(iid)
        rec   = InstanceRecord(
            instance_id = iid,
            ip          = payload["ip"],
            port        = payload["port"],
            public_key  = payload["public_key"],
            human_name  = payload.get("human_name", ""),
            last_seen   = time.time(),
            relay_token = token,
            source      = source,
            via_relay   = via_relay,
            envelope    = json.dumps(envelope) if envelope else None,
        )
        with self._lock:
            self._live[iid] = rec
            self._persist(rec)
        return token

    def find(self, instance_id: str) -> Optional[InstanceRecord]:
        self._expire()
        with self._lock:
            return self._live.get(instance_id)

    def list_live(self) -> list[InstanceRecord]:
        self._expire()
        with self._lock:
            return list(self._live.values())

    def list_for_gossip(self) -> list[dict]:
        """Return signed envelopes for gossip. Only records that carry a
        verifiable envelope are gossipable — plain records (pre-migration
        rows or legacy peers) are omitted so downstream relays never accept
        unverifiable data."""
        out = []
        for r in self.list_live():
            if not r.envelope:
                continue
            try:
                out.append(json.loads(r.envelope))
            except Exception:
                continue
        return out

    def merge_gossip(self, envelopes: list[dict], via_relay: str,
                     verify=None):
        """
        Merge records received from a peer relay.
        Each ``envelope`` must be a signed heartbeat ({payload, signature}) —
        unsigned records are rejected. ``verify`` is the verifier function
        supplied by the app module so this file stays import-clean.
        """
        if verify is None:
            return
        for env in envelopes:
            if not isinstance(env, dict) or "payload" not in env:
                continue
            if not verify(env, config.MAX_AGE_PAYLOAD):
                continue
            payload = env["payload"]
            iid = payload.get("instance_id")
            if not iid:
                continue
            # Binding check: the claimed instance_id must equal the signing
            # public key. Without this, a valid signer could forge another
            # instance's record.
            if payload.get("public_key") != iid:
                continue
            with self._lock:
                existing = self._live.get(iid)
                ts = float(payload.get("timestamp", 0))
                if existing and existing.last_seen >= ts:
                    continue
                if time.time() - ts > config.TTL_SECONDS:
                    continue
                self.register(payload, source="gossip",
                              via_relay=via_relay, envelope=env)

    def _expire(self):
        cutoff = time.time() - config.TTL_SECONDS
        with self._lock:
            stale = [k for k, v in self._live.items()
                     if v.last_seen < cutoff]
            for k in stale:
                del self._live[k]


# Module-level singleton
registry = Registry()
