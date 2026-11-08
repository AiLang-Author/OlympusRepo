"""
olympusrepo/core/import_git.py
Git repository importer — brings existing git history into OlympusRepo
with full fidelity: parent DAG, author/committer identities, timestamps,
tree hashes, and original SHAs are all preserved so the repo can
round-trip back to git later.
Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
MIT License
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from . import db, objects, repo as repo_mod


# Absolute path to git. The web server process inherits its own PATH and
# may not see the user's shell-configured locations (e.g. under WSL or
# systemd), so bare "git" can ENOENT at runtime. shutil.which() honours
# the process PATH; we fall back to /usr/bin/git for the common case.
GIT_BIN = shutil.which("git") or os.environ.get("OLYMPUSREPO_GIT_BIN") or "/usr/bin/git"


# ---------------------------------------------------------------------------
# Subprocess hardening
# ---------------------------------------------------------------------------
# Git has a long history of protocol/submodule-related footguns. These args
# are prepended to every git invocation to shut them down:
#   * protocol.allow=never              - default-deny all transports
#   * protocol.https.allow=always       - re-enable https only
#   * protocol.http.allow=always        - plain http (for internal mirrors)
#   * protocol.file.allow=user          - file:// only when operator opts in
#   * protocol.ext.allow=never          - kills ext:: (historical RCE)
#
# GIT_TERMINAL_PROMPT=0 stops git from blocking on credential prompts that
# cannot be answered by a web request.
GIT_SAFE_ARGS = [
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "protocol.http.allow=always",
    "-c", "protocol.file.allow=user",
    "-c", "protocol.ext.allow=never",
]
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
GIT_TIMEOUT_SECONDS = int(os.environ.get("OLYMPUSREPO_GIT_TIMEOUT", "300"))

# Clone timeout is separate — network clones of large repos (Linux kernel)
# can take 30+ minutes. 0 = no timeout (Python's subprocess.run default).
GIT_CLONE_TIMEOUT = int(os.environ.get("OLYMPUSREPO_GIT_CLONE_TIMEOUT", "0")) or None

# git log over 1M+ commits can take minutes. Give it 10x the normal
# per-command timeout.
GIT_LOG_TIMEOUT = int(os.environ.get("OLYMPUSREPO_GIT_LOG_TIMEOUT",
                                     str(GIT_TIMEOUT_SECONDS * 10))) or None

# Network fetch tuning for remote imports.
#   * A stalled transfer (below lowSpeedLimit bytes/s for lowSpeedTime s)
#     is aborted instead of hanging forever, then retried.
#   * Every fetch is retried with backoff before the import gives up.
FETCH_RETRIES = max(1, int(os.environ.get("OLYMPUSREPO_FETCH_RETRIES", "5")))
FETCH_BLOB_BATCH = max(1, int(os.environ.get("OLYMPUSREPO_FETCH_BLOB_BATCH", "2000")))
FETCH_NET_ARGS = [
    "-c", "http.lowSpeedLimit=" + os.environ.get("OLYMPUSREPO_FETCH_LOW_SPEED_LIMIT", "1000"),
    "-c", "http.lowSpeedTime=" + os.environ.get("OLYMPUSREPO_FETCH_LOW_SPEED_TIME", "60"),
]

# Import guardrails. Set to 0 (the default) for unlimited.
# Operators can cap imports via env vars if needed.
MAX_COMMITS = int(os.environ.get("OLYMPUSREPO_IMPORT_MAX_COMMITS", "0"))  # 0 = unlimited
MAX_TOTAL_BYTES = int(os.environ.get("OLYMPUSREPO_IMPORT_MAX_BYTES", "0"))  # 0 = unlimited

# Paths we never want to suck into the object store. Matched as path
# segments, not raw substrings, so "src/objects/model.py" is NOT skipped
# the way it would be with naive startswith checks.
SKIP_SEGMENTS = frozenset({
    ".venv", "venv", "__pycache__", "node_modules",
    ".git", ".egg-info", ".pytest_cache",
})
SKIP_SUFFIXES = (".pyc", ".pyo")


def _git(args: list, cwd: str, timeout: int = None) -> str:
    """Run a git command and return stdout (text)."""
    if timeout is None:
        timeout = GIT_TIMEOUT_SECONDS
    result = subprocess.run(
        [GIT_BIN, *GIT_SAFE_ARGS, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout or None,
        env=GIT_ENV,
    )
    return result.stdout.strip()


def _path_is_skipped(path: str) -> bool:
    """True if any path segment is in the skip list."""
    if path.endswith(SKIP_SUFFIXES):
        return True
    # Split on forward slash — git always reports POSIX paths from ls-tree.
    return any(seg in SKIP_SEGMENTS for seg in path.split("/"))


# ---------------------------------------------------------------------------
# Commit metadata extraction
# ---------------------------------------------------------------------------
# Record separator (ASCII 0x1e) between fields, unit separator (0x1f)
# between commits. Using control chars instead of "|" means commit
# messages containing the delimiter cannot corrupt parsing.
#
# Fields captured, matching repo_commits columns:
#   %H  commit SHA        -> commit_hash
#   %T  tree SHA          -> tree_hash   (preserving this is what lets us
#                                         round-trip back to git later)
#   %P  parent SHAs       -> parent_hashes[]
#   %an author name       -> author_name
#   %ae author email      -> author_email
#   %at author timestamp  -> authored_at
#   %cn committer name    -> committer_name
#   %ce committer email   -> committer_email
#   %ct committer time    -> committed_at
#   %B  full message      -> message
_COMMIT_FMT = (
    "%H%x1e%T%x1e%P%x1e"
    "%an%x1e%ae%x1e%at%x1e%ai%x1e"   # +%ai (ISO date) for author tz offset
    "%cn%x1e%ce%x1e%ct%x1e%ci%x1e"   # +%ci (ISO date) for committer tz offset
    "%B%x1f"
)


# ISO date format ends in " +HHMM" or " -HHMM". Pull just the offset
# without parsing the rest of the string — git always emits exactly this.
_TZ_RE = re.compile(r"([+\-]\d{4})\s*$")


def _tz_from_iso(iso: str) -> str:
    m = _TZ_RE.search(iso.strip())
    return m.group(1) if m else "+0000"


def _get_commits(git_dir: str, branch: str, max_count: int = None) -> list[dict]:
    """
    Return commits in topological + chronological order (parents before
    children), with full metadata. Commit ordering matters for FK
    integrity: parent_hashes is not FK-enforced in the schema, but we
    want the import to make sense if someone later queries the graph.
    """
    # -n is applied before --reverse, so this keeps the newest max_count
    # commits and still emits them oldest-first.
    limit = ["-n", str(max_count)] if max_count else []
    raw = _git(
        ["log", "--reverse", "--topo-order", *limit,
         f"--format={_COMMIT_FMT}", branch],
        git_dir,
        timeout=GIT_LOG_TIMEOUT,
    )
    commits = []
    for record in raw.split("\x1f"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1e")
        if len(parts) < 12:
            continue
        (sha, tree, parents_str,
         an, ae, at, ai,
         cn, ce, ct, ci,
         message) = parts[:12]
        parents = parents_str.split() if parents_str else []
        commits.append({
            "sha":              sha,
            "tree":             tree,
            "parents":          parents,
            "author_name":      an,
            "author_email":     ae,
            "author_time":      int(at),
            "author_tz":        _tz_from_iso(ai),
            "committer_name":   cn,
            "committer_email":  ce,
            "committer_time":   int(ct),
            "committer_tz":     _tz_from_iso(ci),
            "message":          message,
        })
    return commits


# ---------------------------------------------------------------------------
# File extraction via cat-file --batch
# ---------------------------------------------------------------------------
# Spawning "git show" per-file is O(files * commits) processes. On a
# medium repo that is hundreds of thousands of forks. cat-file --batch
# keeps a single git process alive for the whole import; we feed it
# "<sha>:<path>\n" and read back framed blob data. Roughly 1000x faster
# in practice.
class _CatFileBatch:
    """Long-lived `git cat-file --batch` helper."""

    def __init__(self, git_dir: str):
        self.proc = subprocess.Popen(
            [GIT_BIN, *GIT_SAFE_ARGS, "cat-file", "--batch"],
            cwd=git_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=GIT_ENV,
            bufsize=0,
        )

    def read_blob(self, commit_sha: str, path: str) -> bytes | None:
        """
        Return the blob bytes for `path` at `commit_sha`, or None if the
        object is missing or not a blob.
        """
        req = f"{commit_sha}:{path}\n".encode("utf-8", errors="surrogateescape")
        self.proc.stdin.write(req)
        # Header line: "<sha> <type> <size>\n" OR "<name> missing\n"
        header = self.proc.stdout.readline()
        if not header:
            raise RuntimeError("git cat-file closed unexpectedly")
        header_str = header.decode("utf-8", errors="replace").rstrip("\n")
        if header_str.endswith(" missing"):
            return None
        try:
            _sha, obj_type, size_str = header_str.split()
            size = int(size_str)
        except ValueError:
            return None
        if obj_type != "blob":
            # Read and discard non-blob payload + trailing LF.
            _ = self.proc.stdout.read(size + 1)
            return None
        data = b""
        remaining = size
        while remaining > 0:
            chunk = self.proc.stdout.read(remaining)
            if not chunk:
                raise RuntimeError("git cat-file truncated blob")
            data += chunk
            remaining -= len(chunk)
        # Trailing newline after every object.
        self.proc.stdout.read(1)
        return data

    def read_blob_by_hash(self, blob_hash: str) -> bytes | None:
        """Read a blob directly by its git object hash."""
        req = f"{blob_hash}\n".encode()
        self.proc.stdin.write(req)
        header = self.proc.stdout.readline()
        if not header:
            raise RuntimeError("git cat-file closed unexpectedly")
        header_str = header.decode("utf-8", errors="replace").rstrip("\n")
        if header_str.endswith(" missing"):
            return None
        try:
            _sha, obj_type, size_str = header_str.split()
            size = int(size_str)
        except ValueError:
            return None
        if obj_type != "blob":
            _ = self.proc.stdout.read(size + 1)
            return None
        data = b""
        remaining = size
        while remaining > 0:
            chunk = self.proc.stdout.read(remaining)
            if not chunk:
                raise RuntimeError("git cat-file truncated blob")
            data += chunk
            remaining -= len(chunk)
        self.proc.stdout.read(1)
        return data

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _missing_blobs(mirror_path: str, rev: str) -> list[str]:
    """Blob ids reachable from `rev` that are not present locally.
    --missing=print reports them without triggering a lazy fetch.
    Streamed line by line so a Linux-sized object walk stays flat in memory
    apart from the (much smaller) list of missing ids."""
    proc = subprocess.Popen(
        [GIT_BIN, *GIT_SAFE_ARGS, "rev-list", "--objects",
         "--missing=print", rev, "--"],
        cwd=mirror_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=GIT_ENV, text=True,
    )
    missing = [line[1:].strip() for line in proc.stdout if line.startswith("?")]
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args,
                                            output="", stderr=err)
    return missing


def _backfill_blobs(mirror_path: str, branch, run_fetch, progress_cb=None):
    """Fetch every missing blob for the import branch in batches of
    FETCH_BLOB_BATCH ids, via the promisor remote (fetch --stdin, the
    mechanism git's own lazy fetch uses, but thousands of ids per request)."""
    rev = f"refs/heads/{branch}" if branch else "--all"
    fetch_cmd = [GIT_BIN, *GIT_SAFE_ARGS, *FETCH_NET_ARGS,
                 "-c", "fetch.negotiationAlgorithm=noop",
                 "fetch", "--progress", "--no-tags", "--no-write-fetch-head",
                 "--recurse-submodules=no", "--filter=blob:none",
                 "--stdin", "origin"]
    for rnd in range(1, 4):
        if progress_cb:
            progress_cb(0, 0, "Phase 3: listing missing file contents...")
        print("  Phase 3: listing missing blobs...")
        missing = _missing_blobs(mirror_path, rev)
        if not missing:
            print("  Phase 3 complete — all file contents are local.")
            return
        batches = (len(missing) + FETCH_BLOB_BATCH - 1) // FETCH_BLOB_BATCH
        print(f"  Phase 3: {len(missing)} blobs missing, {batches} batch(es)")
        for b in range(batches):
            chunk = missing[b * FETCH_BLOB_BATCH:(b + 1) * FETCH_BLOB_BATCH]
            label = f"blobs {b + 1}/{batches}"
            if progress_cb:
                progress_cb(b + 1, batches, f"Phase 3: fetching file contents, batch {b + 1}/{batches}")
            run_fetch(None, label, cmd=fetch_cmd,
                      stdin_data="\n".join(chunk) + "\n")
    # Anything still missing after three rounds is lazy-fetched on demand.
    print("  Phase 3: some blobs still missing; they will be fetched on demand.")


def _list_tree(git_dir: str, commit_sha: str) -> list[str]:
    """List all blob paths at a commit, filtered by skip rules."""
    output = _git(
        ["ls-tree", "-r", "--name-only", commit_sha],
        git_dir,
        timeout=GIT_LOG_TIMEOUT,
    )
    return [p for p in output.splitlines()
            if p.strip() and not _path_is_skipped(p)]


# ---------------------------------------------------------------------------
# diff-tree: only files that changed between parent and child
# ---------------------------------------------------------------------------
# git diff-tree -r --no-commit-id -M outputs lines like:
#   :100644 100644 <old_hash> <new_hash> M\tpath
#   :000000 100644 0000000.. <new_hash> A\tpath
#   :100644 000000 <old_hash> 0000000.. D\tpath
#   :100644 100644 <old_hash> <new_hash> R100\told_path\tnew_path
#
# Using -z for NUL termination is safer for paths with special chars.

# Map git diff-tree status letters to our change_type values
_STATUS_MAP = {
    "A": "add",
    "M": "modify",
    "D": "delete",
    "T": "modify",   # type change (e.g. file→symlink) — treat as modify
    "C": "add",      # copy — the new path is effectively an add
}


def _diff_tree(git_dir: str, commit_sha: str, parent_sha: str) -> list[dict]:
    """
    Return list of changes between parent and commit.

    Each entry: {
        "path": str,
        "change_type": "add" | "modify" | "delete" | "rename",
        "old_hash": str | None,     # git blob hash (for reading via cat-file)
        "new_hash": str | None,     # git blob hash
        "old_path": str | None,     # only for renames
    }
    """
    output = _git(
        ["diff-tree", "-r", "--no-commit-id", "-M", "-z",
         parent_sha, commit_sha],
        git_dir,
        timeout=GIT_LOG_TIMEOUT,
    )

    if not output:
        return []

    # -z output: NUL-separated fields. Each record is:
    #   :<old_mode> <new_mode> <old_hash> <new_hash> <status>\0<path>\0
    # For renames: ...<status>\0<old_path>\0<new_path>\0
    parts = output.split("\0")
    changes = []
    i = 0
    while i < len(parts):
        raw = parts[i]
        if not raw.startswith(":"):
            i += 1
            continue

        # Parse: ":old_mode new_mode old_hash new_hash status"
        meta = raw[1:]  # strip leading ':'
        fields = meta.split()
        if len(fields) < 5:
            i += 1
            continue

        _old_mode, _new_mode, old_hash, new_hash, status = fields[:5]
        null_hash = "0" * len(old_hash)

        status_letter = status[0]  # R100 → R, C050 → C, etc.

        if status_letter in ("R", "C"):
            # Rename/copy: two path fields follow
            if i + 2 >= len(parts):
                break
            old_path = parts[i + 1]
            new_path = parts[i + 2]
            i += 3

            if _path_is_skipped(new_path):
                continue
            changes.append({
                "path": new_path,
                "change_type": "rename" if status_letter == "R" else "add",
                "old_hash": old_hash if old_hash != null_hash else None,
                "new_hash": new_hash if new_hash != null_hash else None,
                "old_path": old_path if status_letter == "R" else None,
            })
        else:
            # Single path field follows
            if i + 1 >= len(parts):
                break
            path = parts[i + 1]
            i += 2

            if _path_is_skipped(path):
                continue

            change_type = _STATUS_MAP.get(status_letter, "modify")
            changes.append({
                "path": path,
                "change_type": change_type,
                "old_hash": old_hash if old_hash != null_hash else None,
                "new_hash": new_hash if new_hash != null_hash else None,
                "old_path": None,
            })

    return changes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def import_git_repo(
    conn,
    git_source: str,
    repo_name: str,
    user_id: int,
    objects_dir: str,
    branch: str = None,
    progress_cb=None,
    mirrors_dir: str = None,
    depth: int = None,
) -> dict:
    """
    Import a git repository into OlympusRepo with full metadata fidelity.

    depth: import only the newest `depth` commits of the branch. Remote
    sources are then fetched shallow, so older history is never
    downloaded. The oldest imported commit is stored as a full snapshot.

    Preserves on each repo_commits row:
      * commit_hash    = original git commit SHA (PK)
      * tree_hash      = original git tree SHA
      * parent_hashes  = original parent SHAs (array, supports merges)
      * author_name, author_email, authored_at
      * committer_name, committer_email, committed_at
      * message        = full commit message body
      * is_imported    = TRUE

    Root commits (no parents) store a full-tree snapshot. Subsequent
    commits store only the diff from their first parent, making imports
    O(total_changed_files) instead of O(commits * tree_size).

    Args:
        conn:        DB connection
        git_source:  path to existing git repo OR remote URL
        repo_name:   name for the new OlympusRepo repository
        user_id:     user performing the import (recorded on the repo as
                     imported_by, NOT as author_id on every commit)
        objects_dir: blob store path
        branch:      git branch to import (default: repo's default branch)
        progress_cb: optional callable(current, total, message)

    Returns dict with repo_id, commits_imported, files_imported, branch.
    """
    # Defense in depth: refuse any URL-ish source that begins with "-".
    # Older versions of git mis-parsed leading-dash args as options
    # (CVE-2017-1000117 era).
    if git_source.startswith("-"):
        raise ValueError("git source must not start with '-'")

    # Validate branch before it hits argv. Git refs are fairly permissive
    # but we whitelist a conservative set so no branch name can be
    # interpreted as a flag.
    if branch is not None:
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9/_.\-]{0,199}$', branch):
            raise ValueError(f"Invalid branch name: {branch!r}")

    tmp_dir = None
    git_dir = git_source

    # URL sources get fetched into a persistent bare mirror so that a
    # dropped network connection doesn't lose progress. Unlike git-clone
    # (all-or-nothing), git-fetch keeps every object already transferred.
    # Re-running the import after a drop simply resumes where it left off.
    #
    # Mirror path: <mirrors_dir>/<sanitised_url>.git
    # The mirror is kept after import for future pulls / re-imports.
    if git_source.startswith(("http://", "https://", "git://", "git@")):
        # Build a stable, filesystem-safe mirror directory name from the URL.
        _safe = re.sub(r'[^A-Za-z0-9._-]', '_', git_source.rstrip('/'))
        if len(_safe) > 200:
            _safe = _safe[:200]
        _mirrors_root = mirrors_dir or os.environ.get(
            "OLYMPUSREPO_MIRRORS_DIR",
            os.path.join(os.path.dirname(__file__), "..", "..", "mirrors"),
        )
        os.makedirs(_mirrors_root, exist_ok=True)
        mirror_path = os.path.join(_mirrors_root, f"{_safe}.git")

        # Initialise the bare repo if this is the first attempt.
        if not os.path.isdir(os.path.join(mirror_path, "objects")):
            if progress_cb:
                progress_cb(0, 0, f"Initialising mirror for {git_source}...")
            print(f"  Initialising bare mirror: {mirror_path}")
            os.makedirs(mirror_path, exist_ok=True)
            subprocess.run(
                [GIT_BIN, "init", "--bare"],
                cwd=mirror_path,
                check=True,
                capture_output=True,
                env=GIT_ENV,
            )
        else:
            print(f"  Resuming into existing mirror: {mirror_path}")

        # -------------------------------------------------------------------
        # Blobless incremental fetch — designed for flaky WiFi / drivers
        # that crash under sustained bulk transfers (e.g. Intel AX210).
        #
        # Strategy:
        #   Phase 1 — fetch commits + trees only (--filter=blob:none).
        #             Done in incremental depth stages so each HTTP
        #             transfer is small (KB–low MB).  Completed stages
        #             survive connection drops.
        #   Phase 2 — backfill blob data in bounded size tiers so no
        #             single transfer is large enough to crash the NIC.
        #   Phase 3 — configure promisor remote so any blobs still
        #             missing are lazy-fetched individually during import
        #             (each fetch is one small file).
        #
        # Net effect: no single network transfer exceeds ~20 MB.
        # -------------------------------------------------------------------
        DEEPEN_STEP = int(os.environ.get("OLYMPUSREPO_FETCH_DEEPEN_STEP", "500"))

        def _run_fetch_once(extra_args, label, cmd=None, stdin_data=None):
            """Run a single git-fetch with progress streaming."""
            if cmd is None:
                cmd = [GIT_BIN, *GIT_SAFE_ARGS, *FETCH_NET_ARGS,
                       "fetch", "--progress",
                       "--no-tags", "--no-recurse-submodules",
                       *extra_args,
                       "--", git_source, "+refs/heads/*:refs/heads/*"]
            proc = subprocess.Popen(
                cmd, cwd=mirror_path,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=GIT_ENV, text=True,
            )
            if stdin_data is not None:
                proc.stdin.write(stdin_data)
                proc.stdin.close()
            buf = ""
            while True:
                chunk = proc.stderr.read(256)
                if not chunk:
                    break
                buf += chunk
                lines = buf.replace("\r", "\n").split("\n")
                for line in lines[:-1]:
                    line = line.strip()
                    if line:
                        print(f"  [{label}] {line}")
                        if progress_cb:
                            progress_cb(0, 0, f"[{label}] {line}")
                buf = lines[-1]
            rc = proc.wait()
            if rc != 0:
                raise subprocess.CalledProcessError(rc, cmd, output="", stderr=buf)

        def _run_fetch(extra_args, label, cmd=None, stdin_data=None):
            """git-fetch with retries. Each fetch is all-or-nothing, so a
            dropped or stalled connection (see FETCH_NET_ARGS) is retried
            with backoff instead of failing the whole import."""
            for attempt in range(1, FETCH_RETRIES + 1):
                try:
                    return _run_fetch_once(extra_args, label, cmd, stdin_data)
                except subprocess.CalledProcessError:
                    if attempt == FETCH_RETRIES:
                        raise
                    wait = min(60, 5 * 2 ** (attempt - 1))
                    msg = (f"{label} failed (attempt {attempt}/{FETCH_RETRIES})"
                           f" — retrying in {wait}s")
                    print(f"  {msg}")
                    if progress_cb:
                        progress_cb(0, 0, msg)
                    time.sleep(wait)

        def _is_shallow():
            return os.path.isfile(os.path.join(mirror_path, "shallow"))

        def _has_refs():
            try:
                return bool(_git(["branch", "--list"], mirror_path))
            except Exception:
                return False

        # ---- Phase 1: fetch commits + trees (no blobs) ------------------
        FILTER = ["--filter=blob:none"]
        refs_exist = _has_refs()

        if depth and not (refs_exist and not _is_shallow()):
            # Shallow import: fetch only the newest `depth` commits (no
            # blobs yet). Re-running with a larger depth extends it.
            if progress_cb:
                progress_cb(0, 0, f"Phase 1: fetching last {depth} commits (no blobs)...")
            print(f"  Phase 1: shallow fetch --depth {depth} --filter=blob:none")
            _run_fetch(["--depth", str(depth)] + FILTER, "shallow")
        elif refs_exist and not _is_shallow():
            if progress_cb:
                progress_cb(0, 0, "Updating existing mirror...")
            print(f"  Full history exists — quick update fetch...")
            try:
                _run_fetch(FILTER, "update")
            except subprocess.CalledProcessError:
                print(f"  Update fetch failed — using existing mirror data.")
        else:
            # Stage 1: shallow blobless fetch — branch tips only.
            if not refs_exist:
                if progress_cb:
                    progress_cb(0, 0, "Phase 1/Stage 1: fetching branch tips (no blobs)...")
                print(f"  Phase 1/Stage 1: shallow fetch --depth 1 --filter=blob:none")
                try:
                    _run_fetch(["--depth", "1"] + FILTER, "shallow")
                except subprocess.CalledProcessError:
                    print(f"  Shallow fetch failed — mirror kept at {mirror_path}")
                    print(f"  Re-run the same import command to resume.")
                    raise
                print(f"  Stage 1 complete — branch tips are local.")
            else:
                print(f"  Stage 1 already done — resuming deepening...")

            # Stage 2: deepen in chunks (still blobless).
            pass_num = 0
            while _is_shallow():
                pass_num += 1
                label = f"deepen pass {pass_num}"
                if progress_cb:
                    progress_cb(0, 0, f"Phase 1/Stage 2: {label} (+{DEEPEN_STEP} commits)...")
                print(f"  Phase 1/Stage 2: {label} (--deepen={DEEPEN_STEP})")
                try:
                    _run_fetch(["--deepen", str(DEEPEN_STEP)] + FILTER, label)
                except subprocess.CalledProcessError:
                    print(f"  {label} failed — progress preserved at {mirror_path}")
                    print(f"  Re-run the same import command to resume.")
                    raise
                if pass_num >= 200:
                    print(f"  200 deepen passes — switching to unshallow.")
                    break

            # Stage 3: unshallow (still blobless).
            if _is_shallow():
                if progress_cb:
                    progress_cb(0, 0, "Phase 1/Stage 3: fetching remaining history...")
                print(f"  Phase 1/Stage 3: unshallow --filter=blob:none")
                try:
                    _run_fetch(["--unshallow"] + FILTER, "unshallow")
                except subprocess.CalledProcessError:
                    print(f"  Unshallow failed — progress preserved at {mirror_path}")
                    print(f"  Re-run the same import command to resume.")
                    raise

        if depth:
            print(f"  Phase 1 complete — last {depth} commits are local (no blobs yet).")
        else:
            print(f"  Phase 1 complete — full commit history is local (no blobs yet).")

        # ---- Phase 2: configure the promisor remote ----------------------
        # Needed before Phase 3: fetching blobs by id goes through the
        # "origin" promisor remote, the same way git's own lazy fetch does.
        try:
            _git(["remote", "remove", "origin"], mirror_path)
        except Exception:
            pass
        _git(["remote", "add", "origin", git_source], mirror_path)
        _git(["config", "remote.origin.promisor", "true"], mirror_path)
        _git(["config", "remote.origin.partialCloneFilter", "blob:none"],
             mirror_path)
        _git(["config", "extensions.partialClone", "origin"], mirror_path)

        # ---- Phase 3: backfill blobs in bounded batches ------------------
        # A plain `git fetch --filter=blob:limit=N` cannot backfill: the
        # server sees we already have those commits and sends nothing. So we
        # list the missing blob ids ourselves and request them in batches.
        # Without this, every blob would be lazy-fetched one HTTP request
        # at a time during the import (millions of round trips for Linux).
        # Finished batches stay in the mirror, so a re-run only fetches the
        # blobs still missing.
        _backfill_blobs(mirror_path, branch, _run_fetch, progress_cb)

        print(f"  All phases complete — ready for offline import.")
        git_dir = mirror_path
    else:
        if not os.path.isdir(git_source):
            raise ValueError(
                f"Local git path not found or not a directory: {git_source}"
            )

    batch = None
    repo_id = None
    try:
        # Detect default branch if caller didn't specify one.
        if not branch:
            try:
                branch = _git(
                    ["rev-parse", "--abbrev-ref", "HEAD"],
                    git_dir,
                )
                if branch == "HEAD":
                    # Detached checkout — fall back.
                    branch = "main"
            except Exception:
                branch = "main"

        commits = _get_commits(git_dir, branch, max_count=depth)
        total = len(commits)
        if total == 0:
            raise ValueError(f"No commits found on branch '{branch}'")
        if MAX_COMMITS and total > MAX_COMMITS:
            raise ValueError(
                f"Repo has {total} commits; exceeds limit of {MAX_COMMITS}. "
                f"Set OLYMPUSREPO_IMPORT_MAX_COMMITS=0 for unlimited."
            )
        print(f"  Found {total} commit(s) on branch '{branch}'")

        # Create the destination repo and record provenance at the repo
        # level (not by mutating commit messages). Assumes repo_mod.create_repo
        # accepts the new imported_from/imported_by kwargs wired to the
        # columns added in 015_git_import.sql.
        r = repo_mod.create_repo(
            conn, repo_name, user_id,
            visibility="public",
            imported_from=git_source,
            default_branch=branch,
        )
        repo_id = r["repo_id"]

        batch = _CatFileBatch(git_dir)

        imported = 0
        total_files = 0
        total_bytes = 0
        seen = set()

        for i, c in enumerate(commits):
            subject = c["message"].splitlines()[0] if c["message"] else ""
            # In a partial-history import (depth, or a shallow mirror) a
            # commit's parents may lie outside what we import. Keep only
            # imported parents; a commit with none left becomes a root,
            # stored as a full snapshot, the way git treats shallow commits.
            c["parents"] = [p_ for p_ in c["parents"] if p_ in seen]
            seen.add(c["sha"])
            is_root = not c["parents"]

            if progress_cb:
                progress_cb(i + 1, total, subject[:60])
            elif i % 1000 == 0 or i == total - 1 or is_root:
                # Reduce log spam on huge repos — print every 1000th commit
                print(f"  [{i+1}/{total}] {c['sha'][:8]} {subject[:60]}")

            if is_root:
                # Root commit (no parents) — full snapshot via ls-tree.
                # This is the anchor that materialize_tree walks back to.
                paths = _list_tree(git_dir, c["sha"])
                file_changes = []
                for path in paths:
                    blob = batch.read_blob(c["sha"], path)
                    if blob is None:
                        continue
                    file_changes.append({
                        "path": path,
                        "change_type": "add",
                        "content": blob,
                        "old_path": None,
                    })
                    total_bytes += len(blob)
                    if MAX_TOTAL_BYTES and total_bytes > MAX_TOTAL_BYTES:
                        raise ValueError(
                            f"Import exceeded {MAX_TOTAL_BYTES} bytes; aborting. "
                            f"Set OLYMPUSREPO_IMPORT_MAX_BYTES=0 for unlimited."
                        )
            else:
                # Non-root commit — diff-tree against first parent.
                # Only reads blobs for files that actually changed.
                parent_sha = c["parents"][0]
                diff_entries = _diff_tree(git_dir, c["sha"], parent_sha)

                file_changes = []
                for entry in diff_entries:
                    if entry["change_type"] == "delete":
                        # Deletes don't need blob content
                        file_changes.append({
                            "path": entry["path"],
                            "change_type": "delete",
                            "content": None,
                            "old_path": entry.get("old_path"),
                        })
                        continue

                    # add, modify, rename — read the new blob
                    blob = batch.read_blob(c["sha"], entry["path"])
                    if blob is None:
                        continue
                    file_changes.append({
                        "path": entry["path"],
                        "change_type": entry["change_type"],
                        "content": blob,
                        "old_path": entry.get("old_path"),
                    })
                    total_bytes += len(blob)
                    if MAX_TOTAL_BYTES and total_bytes > MAX_TOTAL_BYTES:
                        raise ValueError(
                            f"Import exceeded {MAX_TOTAL_BYTES} bytes; aborting. "
                            f"Set OLYMPUSREPO_IMPORT_MAX_BYTES=0 for unlimited."
                        )

            repo_mod.import_commit_row(
                conn,
                repo_id=repo_id,
                commit_hash=c["sha"],
                tree_hash=c["tree"],
                parent_hashes=c["parents"],
                author_name=c["author_name"],
                author_email=c["author_email"],
                authored_at_epoch=c["author_time"],
                author_tz_offset=c["author_tz"],
                committer_name=c["committer_name"],
                committer_email=c["committer_email"],
                committed_at_epoch=c["committer_time"],
                committer_tz_offset=c["committer_tz"],
                message=c["message"],
                file_changes=file_changes,
                objects_dir=objects_dir,
            )
            imported += 1
            total_files += len(file_changes)

        # Point the default ref at the tip of the imported branch.
        tip_sha = commits[-1]["sha"]
        repo_mod.set_ref(
            conn,
            repo_id=repo_id,
            ref_name=f"refs/heads/{branch}",
            commit_hash=tip_sha,
            user_id=user_id,
        )

        return {
            "repo_id":          repo_id,
            "repo_name":        repo_name,
            "commits_imported": imported,
            "files_imported":   total_files,
            "bytes_imported":   total_bytes,
            "branch":           branch,
            "tip":              tip_sha,
        }

    except BaseException:
        # create_repo and each imported commit are committed as they go,
        # so a failure here would leave a half-imported repo whose name
        # blocks a retry. Remove it; the mirror keeps the downloaded data,
        # so re-running the import only repeats the local work.
        if repo_id is not None:
            try:
                conn.rollback()
                db.execute(conn, "DELETE FROM repo_repositories WHERE repo_id = %s",
                           (repo_id,))
                print(f"  Import failed — removed partial repo '{repo_name}'.")
            except Exception as cleanup_err:
                print(f"  Import failed and partial repo '{repo_name}' "
                      f"could not be removed: {cleanup_err}")
        raise
    finally:
        if batch is not None:
            batch.close()
        # Mirror directories are intentionally kept for resume / future pulls.
        # Only clean up legacy temp dirs (should no longer be created).
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
