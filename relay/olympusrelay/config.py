# relay/olympusrelay/config.py
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering — MIT

import os
import secrets
import sys

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

_SECRET_FILE = os.environ.get("OLYMPUSRELAY_SECRET_FILE", ".olympusrelay.secret")


def ensure_secret() -> None:
    """Refuse to start with a default/blank secret. If the operator has not
    configured one, either read/generate a persistent file secret (opt-in via
    OLYMPUSRELAY_AUTOGEN=1 — mainly useful for dev) or abort loudly."""
    global SECRET
    if SECRET:
        return
    if os.environ.get("OLYMPUSRELAY_AUTOGEN") == "1":
        try:
            if os.path.exists(_SECRET_FILE):
                with open(_SECRET_FILE) as f:
                    SECRET = f.read().strip()
            if not SECRET:
                SECRET = secrets.token_hex(32)
                fd = os.open(_SECRET_FILE,
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(SECRET)
                print(f"  Generated new relay secret at {_SECRET_FILE}")
            return
        except OSError as e:
            print(f"FATAL: could not persist relay secret: {e}",
                  file=sys.stderr)
            sys.exit(2)
    print(
        "FATAL: OLYMPUSRELAY_SECRET is not set.\n"
        "  Generate one with:  python -c "
        "'import secrets; print(secrets.token_hex(32))'\n"
        "  Export it:          export OLYMPUSRELAY_SECRET=<hex>\n"
        "  Or for dev only:    OLYMPUSRELAY_AUTOGEN=1 olympusrelay",
        file=sys.stderr,
    )
    sys.exit(2)
