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
                via_relay   TEXT
            )
        """)
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
            INSERT INTO instances VALUES
                (:instance_id,:ip,:port,:public_key,:human_name,
                 :last_seen,:relay_token,:source,:via_relay)
            ON CONFLICT(instance_id) DO UPDATE SET
                ip          = excluded.ip,
                port        = excluded.port,
                last_seen   = excluded.last_seen,
                relay_token = excluded.relay_token,
                source      = excluded.source,
                via_relay   = excluded.via_relay
        """, asdict(r))
        self._db.commit()

    # ── Token generation ─────────────────────────────────────────────────

    def _make_token(self, instance_id: str) -> str:
        """
        Deterministic per-instance token derived from relay secret.
        Same token is always issued for the same instance_id so
        re-registrations get the same token without storing state.
        Falls back to a hash of instance_id if no secret configured.
        """
        key = (config.SECRET or "olympusrelay-default-secret").encode()
        return hmac.new(key, instance_id.encode(), hashlib.sha256).hexdigest()

    # ── Public API ───────────────────────────────────────────────────────

    def register(self, payload: dict, source: str = "direct",
                 via_relay: str = None) -> str:
        """
        Register or refresh an instance. Returns the relay_token.
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
        """Return serialisable records for gossip payload."""
        return [
            {
                "instance_id": r.instance_id,
                "ip":          r.ip,
                "port":        r.port,
                "public_key":  r.public_key,
                "human_name":  r.human_name,
                "last_seen":   r.last_seen,
            }
            for r in self.list_live()
        ]

    def merge_gossip(self, records: list[dict], via_relay: str):
        """
        Merge records received from a peer relay.
        Only updates if the incoming last_seen is more recent.
        Does NOT re-gossip (depth-1 only).
        """
        for rec in records:
            iid = rec.get("instance_id")
            if not iid:
                continue
            with self._lock:
                existing = self._live.get(iid)
                if existing and existing.last_seen >= rec["last_seen"]:
                    continue
                # Don't accept records older than TTL
                if time.time() - rec["last_seen"] > config.TTL_SECONDS:
                    continue
                self.register(rec, source="gossip", via_relay=via_relay)

    def _expire(self):
        cutoff = time.time() - config.TTL_SECONDS
        with self._lock:
            stale = [k for k, v in self._live.items()
                     if v.last_seen < cutoff]
            for k in stale:
                del self._live[k]


# Module-level singleton
registry = Registry()
