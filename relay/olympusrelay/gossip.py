# relay/olympusrelay/gossip.py
# Background thread — pushes registry to peer relays every 60s.
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering — MIT

import threading
import time

import httpx

from . import config
from .registry import registry


def _gossip_once():
    records = registry.list_for_gossip()
    if not records:
        return

    relay_id = config.RELAY_ID or "unknown"
    payload  = {"from_relay": relay_id, "instances": records}

    for peer in config.PEERS:
        url = peer.rstrip("/") + "/relay/gossip"
        try:
            r = httpx.post(url, json=payload, timeout=5)
            if r.status_code == 200:
                # Peer may return its own records for us to merge
                data = r.json()
                incoming = data.get("instances", [])
                if incoming:
                    registry.merge_gossip(incoming, via_relay=peer)
        except Exception:
            pass  # peer unreachable — silent, try next cycle


def _loop():
    while True:
        time.sleep(config.GOSSIP_INTERVAL)
        try:
            _gossip_once()
        except Exception:
            pass  # never crash the background thread


def start():
    t = threading.Thread(target=_loop, daemon=True, name="relay-gossip")
    t.start()
