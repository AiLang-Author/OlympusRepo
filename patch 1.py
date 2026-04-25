# ==========================================================================
# FIX 1 — olympusrepo/core/pull_git.py
#
# Replace the hardcoded mirrors_root default in pull_from_git() signature:
#
#   BEFORE:
#     mirrors_root: str = "/var/lib/olympusrepo/mirrors",
#
#   AFTER (two-liner — keep the rest of the signature identical):
# ==========================================================================

#   mirrors_root: str = os.environ.get(
#       "OLYMPUSREPO_MIRRORS_DIR",
#       os.path.join(os.path.dirname(__file__), "..", "..", "mirrors"),
#   ),

# Also apply the same change to _ensure_mirror's os.makedirs call —
# replace the hardcoded string with the passed-in mirrors_root parameter
# (it already uses the parameter internally, so no change needed there).
#
# Full corrected signature line for pull_from_git():
#
#   def pull_from_git(
#       conn, *,
#       repo_id: int,
#       remote_name: str,
#       branch: str,
#       user_id: int,
#       objects_dir: str,
#       mirrors_root: str = os.environ.get(
#           "OLYMPUSREPO_MIRRORS_DIR",
#           os.path.join(os.path.dirname(__file__), "..", "..", "mirrors"),
#       ),
#       progress_cb=None,
#   ) -> dict:


# ==========================================================================
# FIX 2 — setup.sh
#
# In STEP 9 (the .env file write block), add OLYMPUSREPO_MIRRORS_DIR
# alongside OLYMPUSREPO_OBJECTS_DIR.
#
# Find this block in setup.sh:
#
#   OBJECTS_DIR="${INSTALL_ABS}/objects"
#   mkdir -p "$OBJECTS_DIR"
#
# Replace with:
# ==========================================================================

OBJECTS_DIR="${INSTALL_ABS}/objects"
MIRRORS_DIR="${INSTALL_ABS}/mirrors"
mkdir -p "$OBJECTS_DIR"
mkdir -p "$MIRRORS_DIR"

# Then in the cat > "$ENV_FILE" << EOF block, add the line AFTER the
# existing OLYMPUSREPO_OBJECTS_DIR line:
#
#   # Object store — single path used by BOTH CLI and server
#   OLYMPUSREPO_OBJECTS_DIR=${OBJECTS_DIR}
#
#   # Git Bridge mirror cache — bare git mirrors for incremental pull
#   OLYMPUSREPO_MIRRORS_DIR=${MIRRORS_DIR}
#
# And update the setup.sh success message to mention it:

success ".env written (objects: ${OBJECTS_DIR}, mirrors: ${MIRRORS_DIR})"


# ==========================================================================
# FIX 3 — also pass mirrors_root from app.py routes
#
# Any app.py route that calls pull_from_git needs to pass mirrors_root.
# Pattern (add this alongside the objects_dir retrieval):
# ==========================================================================

# mirrors_dir = os.environ.get(
#     "OLYMPUSREPO_MIRRORS_DIR",
#     os.path.join(os.path.dirname(__file__), "..", "..", "mirrors"),
# )
#
# Then in the pull_from_git() call:
#   mirrors_root=mirrors_dir,