#!/usr/bin/env bash
# =============================================================================
# OlympusRepo — One-Shot Setup Script
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
# License: MIT
#
# Usage:
#   ./setup.sh                    # interactive
#   ./setup.sh --mode personal    # skip network prompts
#   ./setup.sh --mode team        # canonical instance, accept offers
#   ./setup.sh --mode contributor # skip server setup, configure remote only
# =============================================================================

set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { /usr/bin/echo -e "${CYAN}→${RESET} $*"; }
success() { /usr/bin/echo -e "${GREEN}✓${RESET} $*"; }
warn()    { /usr/bin/echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { /usr/bin/echo -e "${RED}✗${RESET} $*"; exit 1; }
ask()     { /usr/bin/echo -e "${BOLD}$*${RESET}"; }
divider() { /usr/bin/echo -e "${CYAN}────────────────────────────────────────────────────${RESET}"; }
banner()  {
  /usr/bin/echo ""
  /usr/bin/echo -e "${CYAN}${BOLD}"
  /usr/bin/echo "  ██████╗ ██╗  ██╗   ██╗███╗   ███╗██████╗ ██╗   ██╗███████╗"
  /usr/bin/echo " ██╔═══██╗██║  ╚██╗ ██╔╝████╗ ████║██╔══██╗██║   ██║██╔════╝"
  /usr/bin/echo " ██║   ██║██║   ╚████╔╝ ██╔████╔██║██████╔╝██║   ██║███████╗"
  /usr/bin/echo " ██║   ██║██║    ╚██╔╝  ██║╚██╔╝██║██╔═══╝ ██║   ██║╚════██║"
  /usr/bin/echo " ╚██████╔╝███████╗██║   ██║ ╚═╝ ██║██║     ╚██████╔╝███████║"
  /usr/bin/echo "  ╚═════╝ ╚══════╝╚═╝   ╚═╝     ╚═╝╚═╝      ╚═════╝ ╚══════╝"
  /usr/bin/echo -e "${RESET}"
  /usr/bin/echo -e "  ${BOLD}Sovereign version control. No corporate hooks.${RESET}"
  /usr/bin/echo ""
}

# ── Defaults ─────────────────────────────────────────────────────────────────
MODE=""
INSTALL_DIR="$(pwd)"
DB_NAME="olympusrepo"
DB_USER="olympus"
DB_PASS=""
DB_HOST="127.0.0.1"
DB_PORT="5432"
ZEUS_USER=""
ZEUS_PASS=""
APP_PORT="8000"
PUBLIC_URL=""
NETWORK_MODE=""
ENV_FILE=".env"
SHELL_RC=""

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --mode) MODE="$2"; shift 2 ;;
    --port) APP_PORT="$2"; shift 2 ;;
    --dir)  INSTALL_DIR="$2"; shift 2 ;;
    --db-pass) DB_PASS="$2"; shift 2 ;;
    --zeus-pass) ZEUS_PASS="$2"; shift 2 ;;
    *) warn "Unknown argument: $1"; shift ;;
  esac
done

# =============================================================================
# STEP 0 — Banner + OS detection
# =============================================================================
banner

OS="unknown"
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
  if /usr/bin/grep -qi microsoft /proc/version 2>/dev/null; then
    OS="wsl2"
  else
    OS="linux"
  fi
elif [[ "$OSTYPE" == "darwin"* ]]; then
  OS="macos"
fi

info "Detected OS: ${BOLD}$OS${RESET}"

# Detect shell rc file
if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
  SHELL_RC="$HOME/.zshrc"
elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
  SHELL_RC="$HOME/.bashrc"
else
  SHELL_RC="$HOME/.profile"
fi
info "Shell config: ${BOLD}$SHELL_RC${RESET}"
echo ""

# =============================================================================
# STEP 1 — Mode selection
# =============================================================================
divider
echo -e "${BOLD}  STEP 1 — Instance Mode${RESET}"
divider

if [[ -z "$MODE" ]]; then
  ask "How will you use OlympusRepo?"
  /usr/bin/echo "  1) Personal  — local dev, just you, no network"
  /usr/bin/echo "  2) Team      — canonical instance, others offer changes to you"
  /usr/bin/echo "  3) Contributor — you offer changes to someone else's canonical"
  /usr/bin/echo ""
  read -rp "Enter 1, 2, or 3 [default: 1]: " mode_choice
  case "${mode_choice:-1}" in
    1) MODE="personal" ;;
    2) MODE="team" ;;
    3) MODE="contributor" ;;
    *) warn "Invalid choice, defaulting to personal."; MODE="personal" ;;
  esac
fi

success "Mode: ${BOLD}$MODE${RESET}"
echo ""

# =============================================================================
# STEP 2 — Prerequisites check
# =============================================================================
divider
echo -e "${BOLD}  STEP 2 — Prerequisites${RESET}"
divider

check_cmd() {
  local cmd="$1"; local pkg="$2"; local brew_pkg="${3:-$2}"
  if command -v "$cmd" &>/dev/null; then
    success "$cmd found: $(command -v "$cmd")"
    return 0
  else
    warn "$cmd not found. Attempting install..."
    if [[ "$OS" == "macos" ]]; then
      brew install "$brew_pkg" || error "Could not install $brew_pkg. Install manually."
    elif [[ "$OS" == "linux" || "$OS" == "wsl2" ]]; then
      sudo apt-get install -y "$pkg" || error "Could not install $pkg. Install manually."
    else
      error "$cmd is required. Install it manually."
    fi
    success "$cmd installed."
  fi
}

# Python 3.10+
if command -v python3 &>/dev/null; then
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  PY_MAJOR=$(echo "$PY_VER" | /usr/bin/cut -d. -f1)
  PY_MINOR=$(echo "$PY_VER" | /usr/bin/cut -d. -f2)
  if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 10 ]]; then
    success "python3 $PY_VER found"
  else
    error "Python 3.10+ required. Found $PY_VER. Please upgrade."
  fi
else
  error "python3 not found. Install Python 3.10+ and re-run."
fi

# PostgreSQL
check_cmd psql postgresql postgresql@16
check_cmd diff3 diffutils diffutils
check_cmd git git git

# WSL2 specific: ensure pg service is running
if [[ "$OS" == "wsl2" ]]; then
  if ! pg_isready -h 127.0.0.1 &>/dev/null; then
    info "Starting PostgreSQL (WSL2)..."
    sudo service postgresql start || error "Could not start PostgreSQL."
  fi
  # Offer to auto-start on shell open
  if ! /usr/bin/grep -q "service postgresql start" "$SHELL_RC" 2>/dev/null; then
    read -rp "  Auto-start PostgreSQL when you open a terminal? [Y/n]: " pg_auto
    if [[ "${pg_auto:-Y}" =~ ^[Yy]$ ]]; then
      /usr/bin/echo 'sudo service postgresql start > /dev/null 2>&1' >> "$SHELL_RC"
      success "Added PostgreSQL auto-start to $SHELL_RC"
    fi
  fi
elif [[ "$OS" == "macos" ]]; then
  if ! pg_isready &>/dev/null; then
    info "Starting PostgreSQL (macOS)..."
    brew services start postgresql@16 || error "Could not start PostgreSQL."
    sleep 2
  fi
fi

if ! pg_isready -h 127.0.0.1 &>/dev/null; then
  error "PostgreSQL is not running. Start it and re-run setup."
fi
success "PostgreSQL is running"
echo ""

# =============================================================================
# STEP 3 — Database setup
# =============================================================================
# Passwords are interpolated into SQL single-quoted literals below, so any
# quote/backslash/newline would break out of the literal. We reject those
# characters at input time instead of trying to escape them.
validate_password() {
  local pw="$1"
  [[ ${#pw} -lt 8 ]]       && /usr/bin/echo "too_short"  && return
  [[ "$pw" == *"'"*    ]]  && /usr/bin/echo "bad_quote"  && return
  [[ "$pw" == *'\'*    ]]  && /usr/bin/echo "bad_slash"  && return
  [[ "$pw" == *$'\n'*  ]]  && /usr/bin/echo "bad_nl"     && return
  [[ "$pw" == *$'\r'*  ]]  && /usr/bin/echo "bad_nl"     && return
  /usr/bin/echo "ok"
}

# DB password
ask "PostgreSQL password for user '${DB_USER}':"
DB_PASS=""
while true; do
  read -rp "  Password (min 8 chars, no single-quote or backslash): " DB_PASS; /usr/bin/echo ""
  check=$(validate_password "$DB_PASS")
  case "$check" in
    too_short) warn "Password too short. Try again."; DB_PASS="" ;;
    bad_quote) warn "Password contains a single quote. Try again."; DB_PASS="" ;;
    bad_slash) warn "Password contains a backslash. Try again."; DB_PASS="" ;;
    bad_nl)    warn "Password contains a newline. Try again."; DB_PASS="" ;;
    ok)
      read -rp "  Confirm password: " DB_PASS2; /usr/bin/echo ""
      if [[ "$DB_PASS" != "$DB_PASS2" ]]; then
        warn "Passwords do not match. Try again."; DB_PASS=""
      else
        break
      fi
      ;;
  esac
done

# Custom DB settings?
read -rp "  Use defaults? (db=${DB_NAME}, user=${DB_USER}, host=${DB_HOST}, port=${DB_PORT}) [Y/n]: " db_defaults
if [[ "${db_defaults:-Y}" =~ ^[Nn]$ ]]; then
  read -rp "  DB name [$DB_NAME]: " inp; DB_NAME="${inp:-$DB_NAME}"
  read -rp "  DB user [$DB_USER]: " inp; DB_USER="${inp:-$DB_USER}"
  read -rp "  DB host [$DB_HOST]: " inp; DB_HOST="${inp:-$DB_HOST}"
  read -rp "  DB port [$DB_PORT]: " inp; DB_PORT="${inp:-$DB_PORT}"
fi

# Validate identifiers — they go into SQL unquoted, so anything outside
# a safe Postgres identifier set is rejected.
if ! [[ "$DB_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
  error "DB user must match ^[a-zA-Z_][a-zA-Z0-9_]{0,62}\$ (got '${DB_USER}')."
fi
if ! [[ "$DB_NAME" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
  error "DB name must match ^[a-zA-Z_][a-zA-Z0-9_]{0,62}\$ (got '${DB_NAME}')."
fi

# Create DB user and database.
# Role is created WITHOUT SUPERUSER — limits blast radius if the app is
# ever SQL-injected. pgcrypto (needed for crypt()) is installed into the
# target DB by the postgres superuser after creation.
info "Creating database user '${DB_USER}'..."
sudo -u postgres psql -v ON_ERROR_STOP=1 -c \
  "DO \$\$ BEGIN
     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DB_USER}') THEN
       CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}'
         NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT LOGIN;
     ELSE
       ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASS}'
         NOSUPERUSER NOCREATEDB NOCREATEROLE;
     END IF;
   END \$\$;" 2>/dev/null || {
    # Fallback: maybe we're already the postgres superuser (macOS)
    psql postgres -v ON_ERROR_STOP=1 -c \
      "DO \$\$ BEGIN
         IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DB_USER}') THEN
           CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}'
             NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT LOGIN;
         ELSE
           ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASS}'
             NOSUPERUSER NOCREATEDB NOCREATEROLE;
         END IF;
       END \$\$;" || error "Could not create DB user. Run as sudo or ensure postgres superuser access."
}
success "DB user '${DB_USER}' ready (NOSUPERUSER)"

info "Creating database '${DB_NAME}'..."
DB_EXISTS=""
DB_EXISTS=$(sudo -u postgres psql -tAq -c \
  "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null) \
  || DB_EXISTS=$(psql postgres -tAq -c \
       "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null) \
  || true

if [[ "$DB_EXISTS" == "1" ]]; then
  warn "Database '${DB_NAME}' already exists — skipping create."
else
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME" 2>/dev/null \
    || createdb -h "$DB_HOST" -p "$DB_PORT" -O "$DB_USER" "$DB_NAME" \
    || error "Could not create database '${DB_NAME}'."
  success "Database '${DB_NAME}' created (owner: ${DB_USER})"
fi

# Install pgcrypto as the superuser into the target DB. The app role cannot
# install extensions, which is the whole point of dropping SUPERUSER.
info "Installing pgcrypto extension..."
sudo -u postgres psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -c \
  "CREATE EXTENSION IF NOT EXISTS pgcrypto;" >/dev/null 2>&1 \
  || psql -d "$DB_NAME" postgres -v ON_ERROR_STOP=1 -c \
       "CREATE EXTENSION IF NOT EXISTS pgcrypto;" >/dev/null 2>&1 \
  || error "Could not install pgcrypto extension as postgres superuser."
success "pgcrypto installed"
echo ""

# =============================================================================
# STEP 4 — Run migrations
# =============================================================================
divider
echo -e "${BOLD}  STEP 4 — Migrations${RESET}"
divider

cd "$INSTALL_DIR"

if [[ ! -d "sql" ]]; then
  error "No 'sql/' directory found. Run this script from the OlympusRepo root."
fi

MIGRATION_FILES=$(ls sql/0*.sql 2>/dev/null | /usr/bin/sort)
if [[ -z "$MIGRATION_FILES" ]]; then
  error "No migration files found in sql/. Are you in the right directory?"
fi

FAILED=0
for f in $MIGRATION_FILES; do
  /usr/bin/echo -n "  Running $f ... "
  if PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" \
       -U "$DB_USER" -d "$DB_NAME" -f "$f" > /tmp/olympus_migrate.log 2>&1; then
    /usr/bin/echo -e "${GREEN}ok${RESET}"
  else
    /usr/bin/echo -e "${RED}FAILED${RESET}"
    /usr/bin/cat /tmp/olympus_migrate.log
    FAILED=1
  fi
done

[[ "$FAILED" -eq 0 ]] || error "Migration failed. Fix the error above and re-run."
success "All migrations applied"
echo ""

# =============================================================================
# STEP 5 — Python venv + install
# =============================================================================
divider
echo -e "${BOLD}  STEP 5 — Python Environment${RESET}"
divider

# Venv: mandatory on WSL2, optional on native Linux if packages available
NEED_VENV=0
if [[ "$OS" == "wsl2" ]]; then
  NEED_VENV=1
  info "WSL2 detected — virtual environment required."
elif python3 -c "import fastapi" 2>/dev/null; then
  read -rp "  fastapi found system-wide. Use virtualenv anyway? (recommended) [Y/n]: " use_venv
  [[ "${use_venv:-Y}" =~ ^[Yy]$ ]] && NEED_VENV=1 || NEED_VENV=0
else
  NEED_VENV=1
fi

if [[ "$NEED_VENV" -eq 1 ]]; then
  if [[ ! -d ".venv" ]]; then
    info "Creating virtual environment..."
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON_CMD="$(pwd)/.venv/bin/python3"
  PIP_CMD="$(pwd)/.venv/bin/pip"
  success "Virtual environment ready: .venv"
else
  PYTHON_CMD="python3"
  PIP_CMD="pip3"
  info "Using system Python."
fi

info "Installing OlympusRepo..."
$PIP_CMD install -q --upgrade pip
$PIP_CMD install -q -e .
$PIP_CMD install -q 'uvicorn[standard]'

# Verify
if $PYTHON_CMD -c "import fastapi, uvicorn, psycopg2" 2>/dev/null; then
  success "Python packages installed"
else
  error "Package installation failed. Run manually: pip install -e . 'uvicorn[standard]'"
fi
echo ""

# =============================================================================
# STEP 6 — Zeus account
# Replace the existing Zeus account block in setup.sh with this.
# The function call is wrapped in a DO block so it never prints a
# warning to the user regardless of whether repo_create_user exists.
# =============================================================================

if [[ "$MODE" != "contributor" ]]; then
  ask "Create your Zeus (admin) account:"
  while [[ -z "$ZEUS_USER" ]]; do
    read -rp "  Zeus username (not 'zeus'): " ZEUS_USER
    [[ "$ZEUS_USER" == "zeus" ]] && warn "Don't use 'zeus' — pick your actual username." && ZEUS_USER=""
    if [[ -n "$ZEUS_USER" && ! "$ZEUS_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_.-]{0,62}$ ]]; then
      warn "Username must match ^[a-zA-Z_][a-zA-Z0-9_.-]{0,62}\$"
      ZEUS_USER=""
    fi
    [[ -z "$ZEUS_USER" ]] && warn "Username cannot be empty or invalid."
  done

  if [[ -z "$ZEUS_PASS" ]]; then
    stty sane 2>/dev/null || stty /usr/bin/echo 2>/dev/null || true
    while true; do
      /usr/bin/echo -n "  Zeus password (min 8 chars, visible): "
      read -r PLAIN_TEXT_ENTRY
      ZEUS_PASS="$PLAIN_TEXT_ENTRY"
      check=$(validate_password "$ZEUS_PASS")
      case "$check" in
        too_short) warn "Password too short. Try again."; ZEUS_PASS=""; continue ;;
        bad_quote) warn "Password contains a single quote (breaks SQL)."; ZEUS_PASS=""; continue ;;
        bad_slash) warn "Password contains a backslash (breaks SQL)."; ZEUS_PASS=""; continue ;;
        bad_nl)    warn "Password contains a newline."; ZEUS_PASS=""; continue ;;
      esac
      /usr/bin/echo -n "  Confirm Zeus password: "
      read -r PLAIN_TEXT_ENTRY2
      ZEUS_PASS2="$PLAIN_TEXT_ENTRY2"
      if [[ "$ZEUS_PASS" != "$ZEUS_PASS2" ]]; then
        warn "Passwords do not match. Try again."
        ZEUS_PASS=""
        continue
      fi
      break
    done
  else
    info "Using Zeus password provided via arguments."
    check=$(validate_password "$ZEUS_PASS")
    [[ "$check" != "ok" ]] && error "Zeus password supplied via --zeus-pass fails validation ($check)."
  fi

  # Single SQL block — tries the helper function first, falls back to
  # direct INSERT. All output suppressed. No warning ever reaches stdout.
  PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" \
    -U "$DB_USER" -d "$DB_NAME" -q \
    --tuples-only --no-align \
    << SQL > /dev/null 2>&1
DO \$\$
DECLARE
  _user TEXT := '${ZEUS_USER}';
  _pass TEXT := '${ZEUS_PASS}';
BEGIN
  -- Try the helper function (exists after migrations run)
  BEGIN
    PERFORM repo_create_user(_user, _pass, 'zeus');
  EXCEPTION WHEN undefined_function THEN
    -- Function not available yet — fall through to direct insert
    NULL;
  END;

  -- Upsert directly regardless (idempotent — ensures correct role+hash)
  INSERT INTO repo_users (username, password_hash, role, is_active)
  VALUES (
    _user,
    crypt(_pass, gen_salt('bf', 12)),
    'zeus',
    TRUE
  )
  ON CONFLICT (username) DO UPDATE
    SET password_hash = crypt(_pass, gen_salt('bf', 12)),
        role          = 'zeus',
        is_active     = TRUE;

  -- Deactivate the default seed account if it's different from the new Zeus
  UPDATE repo_users
     SET is_active = FALSE
   WHERE username = 'zeus'
     AND username <> _user;
END;
\$\$;
SQL

  # Check if it worked by querying the user
  USER_CHECK=$(PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" \
    -U "$DB_USER" -d "$DB_NAME" -tAq \
    -c "SELECT username FROM repo_users WHERE username='${ZEUS_USER}' AND is_active=TRUE;" \
    2>/dev/null)

  if [[ "$USER_CHECK" == "$ZEUS_USER" ]]; then
    success "Zeus account '${ZEUS_USER}' created"
  else
    error "Could not create Zeus account '${ZEUS_USER}'. Check DB connection and run migrations first."
  fi

else
  info "Contributor mode — skipping Zeus account setup."
fi

# =============================================================================
# STEP 7 — Relay configuration
# =============================================================================
divider
echo -e "${BOLD}  STEP 7 — OlympusRelay${RESET}"
divider

RELAY_ENABLED_VAL="1"
RELAY_URLS=""
INSTANCE_NAME="$(hostname)"
RUN_LOCAL_RELAY="n"
LOCAL_RELAY_PORT="9000"

if [[ "$MODE" != "contributor" ]]; then
  ask "Relay configuration (enables olympus:// URIs and decentralized discovery):"

  read -rp "  Enable relay registration? [Y/n]: " relay_enable
  if [[ "${relay_enable:-Y}" =~ ^[Nn]$ ]]; then
    RELAY_ENABLED_VAL="0"
    info "Relay disabled. Running in fully private mode."
  else
    # Instance name
    read -rp "  Instance name (shown in relay list) [$INSTANCE_NAME]: " inp
    INSTANCE_NAME="${inp:-$INSTANCE_NAME}"

    # Run a local relay?
    read -rp "  Run a relay node on this instance? [y/N]: " run_relay
    if [[ "${run_relay:-N}" =~ ^[Yy]$ ]]; then
      RUN_LOCAL_RELAY="y"
      read -rp "  Relay port [9000]: " inp
      LOCAL_RELAY_PORT="${inp:-9000}"

      # Install relay package
      info "Installing olympusrelay..."
      if [[ -d "relay" ]]; then
        $PIP_CMD install -q -e relay/
        success "olympusrelay installed"

        # Write start-relay.sh
        /usr/bin/cat > start-relay.sh << RELAYSH
#!/usr/bin/env bash
# Start OlympusRelay
# Generated by setup.sh
cd "$(pwd)"
source .venv/bin/activate 2>/dev/null || true
set -a; source .env 2>/dev/null; set +a
exec olympusrelay --port ${LOCAL_RELAY_PORT} --id "${INSTANCE_NAME}-relay"
RELAYSH
        /usr/bin/chmod +x start-relay.sh
        success "start-relay.sh written — run it to start your relay node"

        # Local relay is the first in the list
        RELAY_URLS="http://localhost:${LOCAL_RELAY_PORT}"
      else
        warn "relay/ directory not found — skipping olympusrelay install."
        warn "Run 'pip install -e relay/' manually after setup."
      fi
    fi

    # Additional relay URLs
    if [[ "$RUN_LOCAL_RELAY" == "y" ]]; then
      read -rp "  Add community/peer relay URLs? (comma-separated, blank to skip): " extra_relays
      if [[ -n "$extra_relays" ]]; then
        RELAY_URLS="${RELAY_URLS},${extra_relays}"
      fi
    else
      /usr/bin/echo "  Community bootstrap relays will be used by default."
      /usr/bin/echo "  Add your own: https://relay1.olympus.community (best-effort)"
      read -rp "  Custom relay URLs? (comma-separated, blank for bootstrap): " custom_relays
      RELAY_URLS="${custom_relays:-}"
    fi

    success "Relay configured"
  fi
else
  # Contributor mode — ask for relay URL to /usr/bin/find canonical
  read -rp "  Relay URL for discovering instances (blank to skip): " contrib_relay
  RELAY_URLS="${contrib_relay:-}"
  [[ -n "$RELAY_URLS" ]] && success "Relay URL: $RELAY_URLS" || info "No relay configured."
fi

echo ""

# =============================================================================
# STEP 8 — Network / connectivity
# =============================================================================
divider
echo -e "${BOLD}  STEP 8 — Network${RESET}"
divider

if [[ "$MODE" == "personal" ]]; then
  PUBLIC_URL="http://localhost:${APP_PORT}"
  info "Personal mode — binding to localhost only."

elif [[ "$MODE" == "team" ]]; then
  ask "How should contributors reach this instance?"
  /usr/bin/echo "  1) Tailscale    — private mesh network, easy setup, recommended"
  /usr/bin/echo "  2) Manual       — you have a domain or static IP"
  /usr/bin/echo "  3) Tor          — .onion hidden service, anonymous, slower"
  /usr/bin/echo "  4) Skip for now — I'll set OLYMPUSREPO_PUBLIC_URL manually later"
  /usr/bin/echo ""
  read -rp "Enter 1-4 [default: 1]: " net_choice

  case "${net_choice:-1}" in
    1)
      NETWORK_MODE="tailscale"
      if ! command -v tailscale &>/dev/null; then
        warn "Tailscale not installed."
        /usr/bin/echo ""
        /usr/bin/echo "  Install Tailscale:"
        if [[ "$OS" == "macos" ]]; then
          /usr/bin/echo "    brew install tailscale"
          /usr/bin/echo "    or: https://tailscale.com/download/mac"
        else
          /usr/bin/echo "    curl -fsSL https://tailscale.com/install.sh | sh"
        fi
        /usr/bin/echo ""
        read -rp "  Install and authenticate Tailscale now, then press Enter to continue..."
      fi
      if command -v tailscale &>/dev/null; then
        TS_IP=$(tailscale ip -4 2>/dev/null || /usr/bin/echo "")
        if [[ -n "$TS_IP" ]]; then
          PUBLIC_URL="http://${TS_IP}:${APP_PORT}"
          success "Tailscale IP: ${TS_IP}"
          success "Contributors will use: ${PUBLIC_URL}"
        else
          warn "Tailscale is installed but not authenticated. Run 'tailscale up' then update OLYMPUSREPO_PUBLIC_URL in .env"
          PUBLIC_URL="http://YOUR_TAILSCALE_IP:${APP_PORT}"
        fi
      else
        warn "Skipping Tailscale setup — set OLYMPUSREPO_PUBLIC_URL in .env later."
        PUBLIC_URL="http://YOUR_TAILSCALE_IP:${APP_PORT}"
      fi
      ;;
    2)
      NETWORK_MODE="manual"
      read -rp "  Your public domain or IP (e.g. olympus.example.com or 1.2.3.4): " pub_host
      read -rp "  Use HTTPS? [y/N]: " use_https
      if [[ "${use_https:-N}" =~ ^[Yy]$ ]]; then
        PUBLIC_URL="https://${pub_host}"
        warn "Remember to set up a reverse proxy (nginx/Caddy) with TLS termination."
        warn "Set OLYMPUSREPO_COOKIE_SECURE=1 in .env for HTTPS."
      else
        PUBLIC_URL="http://${pub_host}:${APP_PORT}"
      fi
      success "Public URL: ${PUBLIC_URL}"
      ;;
    3)
      NETWORK_MODE="tor"
      if ! command -v tor &>/dev/null; then
        warn "Tor not installed. Installing..."
        if [[ "$OS" == "macos" ]]; then
          brew install tor || error "Could not install tor."
        else
          sudo apt-get install -y tor || error "Could not install tor."
        fi
      fi
      # Create hidden service config
      TOR_HS_DIR="/var/lib/tor/olympusrepo"
      sudo /usr/bin/mkdir -p "$TOR_HS_DIR"
      sudo /usr/bin/chmod 700 "$TOR_HS_DIR"
      # Append to torrc if not already there
      if ! /usr/bin/grep -q "olympusrepo" /etc/tor/torrc 2>/dev/null; then
        /usr/bin/echo "" | sudo tee -a /etc/tor/torrc > /dev/null
        /usr/bin/echo "HiddenServiceDir ${TOR_HS_DIR}" | sudo tee -a /etc/tor/torrc > /dev/null
        /usr/bin/echo "HiddenServicePort 80 127.0.0.1:${APP_PORT}" | sudo tee -a /etc/tor/torrc > /dev/null
      fi
      info "Starting Tor hidden service (this may take a minute)..."
      if [[ "$OS" == "linux" || "$OS" == "wsl2" ]]; then
        sudo service tor restart || sudo systemctl restart tor || warn "Could not restart tor automatically."
      elif [[ "$OS" == "macos" ]]; then
        brew services restart tor
      fi
      sleep 5
      ONION_ADDR=""
      if [[ -f "${TOR_HS_DIR}/hostname" ]]; then
        ONION_ADDR=$(sudo /usr/bin/cat "${TOR_HS_DIR}/hostname")
        PUBLIC_URL="http://${ONION_ADDR}"
        success "Tor hidden service: ${ONION_ADDR}"
        warn "Share this .onion address with contributors — do not post it publicly if you want pseudonymity."
      else
        warn "Tor hidden service hostname not ready yet. Check ${TOR_HS_DIR}/hostname after Tor fully starts."
        warn "Update OLYMPUSREPO_PUBLIC_URL in .env once it's available."
        PUBLIC_URL="http://YOUR_ONION_ADDRESS.onion"
      fi
      ;;
    4)
      NETWORK_MODE="manual"
      PUBLIC_URL="http://localhost:${APP_PORT}"
      warn "Skipped network setup. Set OLYMPUSREPO_PUBLIC_URL in .env before sharing with contributors."
      ;;
  esac

elif [[ "$MODE" == "contributor" ]]; then
  ask "What is the canonical instance URL you will offer changes to?"
  read -rp "  Canonical URL (e.g. http://192.168.x.x:8000 or http://abc.onion): " PUBLIC_URL
  PUBLIC_URL="${PUBLIC_URL:-http://localhost:8000}"
fi

echo ""

# =============================================================================
# STEP 9 — Write .env file
# =============================================================================
divider
echo -e "${BOLD}  STEP 9 — Writing .env${RESET}"
divider

INSTALL_ABS="$(cd "$INSTALL_DIR" && pwd)"
OBJECTS_DIR="${INSTALL_ABS}/objects"
MIRRORS_DIR="${INSTALL_ABS}/mirrors"
GATEWAYS_DIR="${INSTALL_ABS}/gateways"
mkdir -p "$OBJECTS_DIR"
mkdir -p "$MIRRORS_DIR"
mkdir -p "$GATEWAYS_DIR"
ALIAS_NAME="olympus-$(basename "$INSTALL_ABS" | /usr/bin/tr '[:upper:]' '[:lower:]' | /usr/bin/tr ' ' '-')"

cat > "$ENV_FILE" << EOF
# OlympusRepo configuration
# Generated by setup.sh on $(date)
# Mode: ${MODE}

# Database
OLYMPUSREPO_DB_NAME=${DB_NAME}
OLYMPUSREPO_DB_USER=${DB_USER}
OLYMPUSREPO_DB_PASS=${DB_PASS}
OLYMPUSREPO_DB_HOST=${DB_HOST}
OLYMPUSREPO_DB_PORT=${DB_PORT}

# Server
OLYMPUSREPO_PORT=${APP_PORT}
OLYMPUSREPO_PUBLIC_URL=${PUBLIC_URL}

# Object store — single path used by BOTH CLI and server
# This is critical: CLI and server must share the same objects directory
OLYMPUSREPO_OBJECTS_DIR=${OBJECTS_DIR}

# Git Bridge mirror cache — bare git mirrors for incremental pull
OLYMPUSREPO_MIRRORS_DIR=${MIRRORS_DIR}

# Smart-HTTP gateways — bare repos that git-upload-pack/receive-pack
# operate on. Derived state, rebuilt on demand from canonical commits.
OLYMPUSREPO_GATEWAYS_ROOT=${GATEWAYS_DIR}

# Security (set to 1 if behind HTTPS reverse proxy)
OLYMPUSREPO_COOKIE_SECURE=0

# Relay
OLYMPUSREPO_RELAY_ENABLED=${RELAY_ENABLED_VAL}
OLYMPUSREPO_INSTANCE_NAME=${INSTANCE_NAME}
OLYMPUSREPO_RELAYS=${RELAY_URLS}
EOF

success ".env written (objects: ${OBJECTS_DIR}, mirrors: ${MIRRORS_DIR}, gateways: ${GATEWAYS_DIR})"

# Source env vars into current shell session
set -a; source "$ENV_FILE"; set +a

# Offer to add env export to shell rc
read -rp "  Source .env automatically from $SHELL_RC? [Y/n]: " add_rc
if [[ "${add_rc:-Y}" =~ ^[Yy]$ ]]; then
  RC_ENTRY="set -a; source \"${INSTALL_ABS}/${ENV_FILE}\" 2>/dev/null; set +a"
  if ! /usr/bin/grep -q "OlympusRepo env" "$SHELL_RC" 2>/dev/null; then
    /usr/bin/echo "" >> "$SHELL_RC"
    /usr/bin/echo "# OlympusRepo env" >> "$SHELL_RC"
    /usr/bin/echo "$RC_ENTRY" >> "$SHELL_RC"
    success "Added env sourcing to $SHELL_RC"
  else
    info "Already present in $SHELL_RC — skipping"
  fi
fi

# Write instance alias to shellrc
VENV_ACTIVATE=""
[[ "$NEED_VENV" -eq 1 ]] && VENV_ACTIVATE="source \"${INSTALL_ABS}/.venv/bin/activate\" && "
ALIAS_CMD="alias ${ALIAS_NAME}=\"${VENV_ACTIVATE}set -a; source \\\"${INSTALL_ABS}/${ENV_FILE}\\\"; set +a\""
if ! /usr/bin/grep -q "$ALIAS_NAME" "$SHELL_RC" 2>/dev/null; then
  /usr/bin/echo "" >> "$SHELL_RC"
  /usr/bin/echo "# OlympusRepo instance alias" >> "$SHELL_RC"
  /usr/bin/echo "$ALIAS_CMD" >> "$SHELL_RC"
  success "Added alias '${ALIAS_NAME}' to $SHELL_RC"
  info "Run '${ALIAS_NAME}' in any terminal to activate this instance"
fi
echo ""

# =============================================================================
# STEP 10 — Update instance_url in DB + run cascade migrations
# =============================================================================
PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
  -c "UPDATE repo_server_config SET value='${PUBLIC_URL}' WHERE key='instance_url';" > /dev/null 2>&1 || true

# Run cascade fix migration if not already applied
PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" << 'SQL' > /dev/null 2>&1 || true
ALTER TABLE repo_commits ALTER COLUMN rev DROP NOT NULL;
ALTER TABLE repo_commits ALTER COLUMN rev DROP DEFAULT;
ALTER TABLE repo_file_revisions ALTER COLUMN global_rev DROP NOT NULL;
ALTER TABLE repo_offers ALTER COLUMN from_rev DROP NOT NULL;
ALTER TABLE repo_offers ALTER COLUMN base_rev DROP NOT NULL;
SQL




# =============================================================================
# STEP 10b — Optional systemd unit install (Linux only, not WSL2)
# =============================================================================
if [[ "$OS" == "linux" ]] && [[ "$MODE" != "contributor" ]]; then
  divider
  echo -e "${BOLD}  STEP 10b — systemd service (optional)${RESET}"
  divider

  read -rp "  Install as a systemd service so it starts on boot? [y/N]: " inst_svc
  if [[ "${inst_svc:-N}" =~ ^[Yy]$ ]]; then
    if [[ ! -f "systemd/olympusrepo.service.template" ]]; then
      warn "systemd/olympusrepo.service.template not found in repo. Skipping."
    else
      UNIT_FILE="/etc/systemd/system/olympusrepo.service"
      info "Writing ${UNIT_FILE}..."
      sudo bash -c "sed \
        -e 's|__USER__|${USER}|g' \
        -e 's|__INSTALL_DIR__|${INSTALL_ABS}|g' \
        -e 's|__APP_PORT__|${APP_PORT}|g' \
        systemd/olympusrepo.service.template > ${UNIT_FILE}"
      sudo chmod 644 "$UNIT_FILE"
      sudo systemctl daemon-reload
      sudo systemctl enable olympusrepo.service
      success "Service installed and enabled"
      info "Start it with: sudo systemctl start olympusrepo"
      info "Check status:  sudo systemctl status olympusrepo"
      info "View logs:     sudo journalctl -u olympusrepo -f"
    fi
  fi
  echo ""
fi


# =============================================================================
# STEP 11 — Contributor remote config
# =============================================================================
if [[ "$MODE" == "contributor" ]]; then
  divider
  /usr/bin/echo -e "${BOLD}  STEP 11 — Contributor Remote${RESET}"
  divider
  info "To start offering changes, clone a repo and add your canonical remote:"
  /usr/bin/echo ""
  /usr/bin/echo -e "  ${CYAN}olympusrepo clone ${PUBLIC_URL}/repo/REPONAME${RESET}"
  /usr/bin/echo -e "  ${CYAN}cd REPONAME${RESET}"
  /usr/bin/echo -e "  ${CYAN}olympusrepo remote add origin ${PUBLIC_URL}${RESET}"
  /usr/bin/echo ""
fi

# =============================================================================
# STEP 12 — Final summary
# =============================================================================
divider
echo ""
echo -e "${GREEN}${BOLD}  ⚡ OlympusRepo is ready.${RESET}"
divider
echo ""
echo -e "  ${BOLD}Activate this instance:${RESET}"
echo ""
echo -e "  ${CYAN}${ALIAS_NAME}${RESET}   (alias added to $SHELL_RC)"
echo ""
echo -e "  ${BOLD}Start the server:${RESET}"
echo ""
if [[ "$NEED_VENV" -eq 1 ]]; then
  /usr/bin/echo -e "  ${CYAN}source .venv/bin/activate${RESET}"
fi
echo -e "  ${CYAN}uvicorn olympusrepo.web.app:app --host 0.0.0.0 --port ${APP_PORT} --reload${RESET}"
echo ""
echo -e "  ${BOLD}Open in browser:${RESET}  ${PUBLIC_URL}"
echo ""

if [[ "$MODE" != "contributor" && -n "$ZEUS_USER" ]]; then
  /usr/bin/echo -e "  ${BOLD}Login:${RESET}  ${ZEUS_USER} / (password you set)"
fi

echo ""
echo -e "  ${BOLD}Create your first repo:${RESET}"
echo -e "  ${CYAN}mkdir myproject && cd myproject${RESET}"
echo -e "  ${CYAN}olympusrepo init myproject${RESET}"
echo -e "  ${CYAN}echo '# My Project' > README.md${RESET}"
echo -e "  ${CYAN}olympusrepo add .${RESET}"
echo -e "  ${CYAN}olympusrepo commit -m 'initial commit'${RESET}"
echo ""

if [[ "$MODE" == "team" ]]; then
  /usr/bin/echo -e "  ${BOLD}Share with contributors:${RESET}"
  /usr/bin/echo -e "  They clone with:  ${CYAN}olympusrepo clone ${PUBLIC_URL}/repo/REPONAME${RESET}"
  /usr/bin/echo -e "  They offer with:  ${CYAN}olympusrepo offer -m \"reason\"${RESET}"
  /usr/bin/echo ""
fi

if [[ "$MODE" == "contributor" ]]; then
  /usr/bin/echo -e "  ${BOLD}Clone and contribute:${RESET}"
  /usr/bin/echo -e "  ${CYAN}olympusrepo clone ${PUBLIC_URL}/repo/REPONAME mylocal${RESET}"
  /usr/bin/echo -e "  ${CYAN}cd mylocal${RESET}"
  /usr/bin/echo -e "  ${CYAN}# make changes${RESET}"
  /usr/bin/echo -e "  ${CYAN}olympusrepo add . && olympusrepo commit -m 'my fix'${RESET}"
  /usr/bin/echo -e "  ${CYAN}olympusrepo offer -m 'why this should be accepted'${RESET}"
  /usr/bin/echo ""
fi

if [[ "$MODE" == "team" && "$NETWORK_MODE" == "tailscale" ]]; then
  /usr/bin/echo -e "  ${YELLOW}Tailscale note:${RESET} Contributors need to be on your Tailnet."
  /usr/bin/echo -e "  Invite them at: https://login.tailscale.com/admin/users"
  /usr/bin/echo ""
fi

if [[ "$MODE" == "team" && "$NETWORK_MODE" == "tor" && -n "${ONION_ADDR:-}" ]]; then
  /usr/bin/echo -e "  ${YELLOW}Tor note:${RESET} Contributors need Tor Browser or torsocks."
  /usr/bin/echo -e "  Hidden service:  ${ONION_ADDR}"
  /usr/bin/echo ""
fi

echo -e "  ${BOLD}Reload shell to use alias:${RESET}"
echo -e "  ${CYAN}source ${SHELL_RC}${RESET}"
echo ""
divider
echo ""
