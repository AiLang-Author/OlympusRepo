"""
olympusrepo/setup_wizard.py

Invoked by: olympusrepo-setup
Installed via PyPI: pip install olympusrepo && olympusrepo-setup

Downloads setup.sh from GitHub and runs it, OR runs the bundled
copy if it shipped with the package (git clone path).

This is the "5 year old illiterate" entry point.
"""

import os
import subprocess
import sys
import tempfile
import urllib.request


SETUP_SH_URL = (
    "https://raw.githubusercontent.com/"
    "AiLang-Author/OlympusRepo/main/setup.sh"
)


def main():
    # ── Check for bash ────────────────────────────────────────────────────
    bash = _find_bash()
    if not bash:
        print("ERROR: bash is required to run the setup wizard.")
        print("  On Windows: install WSL2 first — https://learn.microsoft.com/en-us/windows/wsl/install")
        print("  On macOS:   bash is pre-installed")
        print("  On Linux:   sudo apt install bash")
        sys.exit(1)

    # ── Find setup.sh ─────────────────────────────────────────────────────
    # 1. Bundled alongside this file (git clone)
    here       = os.path.dirname(os.path.abspath(__file__))
    repo_root  = os.path.dirname(here)
    local_sh   = os.path.join(repo_root, "setup.sh")

    if os.path.exists(local_sh):
        print("  Found setup.sh in repo — running locally.")
        _run(bash, local_sh, cwd=repo_root)
        return

    # 2. Download from GitHub (PyPI install path)
    print("  Downloading setup.sh from GitHub...")
    try:
        with urllib.request.urlopen(SETUP_SH_URL, timeout=15) as resp:
            content = resp.read()
    except Exception as e:
        print(f"ERROR: Could not download setup.sh: {e}")
        print(f"  Try manually: curl -fsSL {SETUP_SH_URL} | bash")
        sys.exit(1)

    # Write to a temp file and run from CWD
    with tempfile.NamedTemporaryFile(
            suffix=".sh", delete=False, mode="wb") as f:
        f.write(content)
        tmp_path = f.name

    try:
        os.chmod(tmp_path, 0o755)
        _run(bash, tmp_path, cwd=os.getcwd())
    finally:
        os.unlink(tmp_path)


def _find_bash():
    for candidate in ["/usr/bin/bash", "/bin/bash", "bash"]:
        try:
            subprocess.run(
                [candidate, "--version"],
                capture_output=True, check=True)
            return candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


def _run(bash, script_path, cwd):
    try:
        result = subprocess.run(
            [bash, script_path] + sys.argv[1:],
            cwd=cwd)
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
