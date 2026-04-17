# relay/olympusrelay/config.py
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering — MIT

import os

PORT        = int(os.environ.get("OLYMPUSRELAY_PORT", 9000))
PEERS       = [p.strip() for p in
               os.environ.get("OLYMPUSRELAY_PEERS", "").split(",")
               if p.strip()]
RELAY_ID    = os.environ.get("OLYMPUSRELAY_ID", "")          # human name
DB_PATH     = os.environ.get("OLYMPUSRELAY_DB", "relay.db")  # SQLite path
SECRET      = os.environ.get("OLYMPUSRELAY_SECRET", "")      # for HMAC tokens
TTL_SECONDS = int(os.environ.get("OLYMPUSRELAY_TTL", 600))   # 10 min expiry
GOSSIP_INTERVAL = int(os.environ.get("OLYMPUSRELAY_GOSSIP", 60))  # seconds
MAX_AGE_PAYLOAD = 120  # reject heartbeats older than this
