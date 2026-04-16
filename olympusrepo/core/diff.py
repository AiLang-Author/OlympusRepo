
# Database connection and query helpers
# Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering 
# MIT License
#
# Uses Python's difflib for diff.
# Uses system diff3 for three-way merge (required for merges).
# If diff3 isn't available, merge operations fail loudly rather than silently
# producing wrong output.

import difflib
import os
import subprocess
import tempfile


CONFLICT_MARKER_OURS = "<<<<<<< OURS"
CONFLICT_MARKER_SEP = "======="
CONFLICT_MARKER_THEIRS = ">>>>>>> THEIRS"


class MergeToolMissingError(RuntimeError):
    """Raised when diff3 is not available and a merge is attempted."""
    pass


def unified_diff(old_lines: list[str], new_lines: list[str],
                 old_name: str = "old", new_name: str = "new",
                 context: int = 3) -> str:
    """Generate a unified diff string."""
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=old_name, tofile=new_name,
        lineterm="", n=context
    )
    return "\n".join(diff)


def diff_files(old_path: str, new_path: str) -> tuple[str, int, int]:
    """Diff two files. Returns (diff_text, lines_added, lines_removed)."""
    with open(old_path, "r", errors="replace") as f:
        old_lines = f.readlines()
    with open(new_path, "r", errors="replace") as f:
        new_lines = f.readlines()

    diff_text = unified_diff(old_lines, new_lines, old_path, new_path)

    added = 0
    removed = 0
    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    return diff_text, added, removed


def diff_content(old_content: str, new_content: str,
                 old_name: str = "old", new_name: str = "new") -> tuple[str, int, int]:
    """Diff two strings. Returns (diff_text, lines_added, lines_removed)."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff_text = unified_diff(old_lines, new_lines, old_name, new_name)

    added = sum(1 for l in diff_text.split("\n") if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_text.split("\n") if l.startswith("-") and not l.startswith("---"))

    return diff_text, added, removed


# =========================================================================
# THREE-WAY MERGE
# =========================================================================

def has_diff3() -> bool:
    """Check if diff3 is available on this system."""
    try:
        subprocess.run(["diff3", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def merge_three_way(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """
    Three-way merge using system diff3.

    Args:
        base: content of the common ancestor
        ours: content of our version
        theirs: content of their version

    Returns:
        (merged_content, has_conflicts)

    Raises:
        MergeToolMissingError: if diff3 is not available on the system.

    Fast paths:
        - If ours == theirs, return ours (no conflict).
        - If ours == base, return theirs (they changed, we didn't).
        - If theirs == base, return ours (we changed, they didn't).
    """
    # Fast paths — no actual merge needed
    if ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if theirs == base:
        return ours, False

    if not has_diff3():
        raise MergeToolMissingError(
            "The 'diff3' tool is required for three-way merges but was not found.\n"
            "  Install it: apt-get install diffutils  (Debian/Ubuntu)\n"
            "              brew install diffutils     (macOS)\n"
            "              yum install diffutils      (RHEL/Fedora)"
        )

    return _merge_diff3(base, ours, theirs)


def _merge_diff3(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Three-way merge using system diff3."""
    paths = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".base", delete=False) as f:
            f.write(base)
            paths.append(f.name)
            base_path = f.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".ours", delete=False) as f:
            f.write(ours)
            paths.append(f.name)
            ours_path = f.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".theirs", delete=False) as f:
            f.write(theirs)
            paths.append(f.name)
            theirs_path = f.name

        result = subprocess.run(
            ["diff3", "-m",
             "-L", "OURS",
             "-L", "BASE",
             "-L", "THEIRS",
             ours_path, base_path, theirs_path],
            capture_output=True, text=True, timeout=30
        )

        # diff3 exit codes: 0 = no conflicts, 1 = conflicts, 2 = trouble
        if result.returncode >= 2:
            raise RuntimeError(f"diff3 failed: {result.stderr}")

        merged = result.stdout
        has_conflicts = result.returncode == 1

        return merged, has_conflicts

    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


# =========================================================================
# CONFLICT DETECTION
# =========================================================================

def has_conflict_markers(content: str) -> bool:
    """Check if a file contains unresolved conflict markers."""
    return ("<<<<<<<" in content and
            "=======" in content and
            ">>>>>>>" in content)


def count_conflicts(content: str) -> int:
    """Count the number of conflict blocks in a file."""
    # Count opening markers — diff3 uses "<<<<<<< OURS" by default
    count = 0
    for line in content.split("\n"):
        if line.startswith("<<<<<<<"):
            count += 1
    return count
