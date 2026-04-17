# olympusrepo/relay_bootstrap.py
# Hardcoded community relay list — last-resort fallback when no relays
# are configured in .env.
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering — MIT
#
# These are best-effort community relays. No guaranteed uptime.
# Run your own: pip install olympusrelay && olympusrelay
# Add it to OLYMPUSREPO_RELAYS in your .env.

BOOTSTRAP_RELAYS = [
    "https://relay1.olympus.community",
    "https://relay2.olympus.community",
]


def get_relay_list() -> list[str]:
    """
    Return the relay list for this instance.
    Priority:
      1. OLYMPUSREPO_RELAYS env var (comma-separated)
      2. Hardcoded bootstrap list
    Never returns an empty list — bootstrap is always the fallback.
    """
    import os
    env = os.environ.get("OLYMPUSREPO_RELAYS", "").strip()
    if env:
        relays = [r.strip() for r in env.split(",") if r.strip()]
        if relays:
            return relays
    return BOOTSTRAP_RELAYS
