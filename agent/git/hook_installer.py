"""Installs the code review agent as a git pre-commit hook."""

import os
import platform
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

from agent.utils.logger import get_logger

logger = get_logger(__name__)


def _save_api_key(key: str) -> None:
    """Persist GROQ_API_KEY as a permanent system environment variable."""
    system = platform.system()
    if system == "Windows":
        subprocess.run(["setx", "GROQ_API_KEY", key], check=True, capture_output=True)
        os.environ["GROQ_API_KEY"] = key  # also apply to current session
        print("[OK] GROQ_API_KEY saved to Windows environment variables.")
    else:
        # Mac / Linux — append to shell profile
        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            profile = Path.home() / ".zshrc"
        else:
            profile = Path.home() / ".bashrc"
        line = f'\nexport GROQ_API_KEY="{key}"\n'
        with open(profile, "a", encoding="utf-8") as f:
            f.write(line)
        os.environ["GROQ_API_KEY"] = key  # also apply to current session
        print(f"[OK] GROQ_API_KEY saved to {profile}")
        print(f"[INFO] Run: source {profile}  (or open a new terminal)")


def _prompt_api_key() -> None:
    """Ask the user for their Groq API key and save it if not already set."""
    existing = os.environ.get("GROQ_API_KEY")
    if existing:
        print(f"[INFO] GROQ_API_KEY is already set ({existing[:8]}...).")
        answer = input("       Do you want to replace it? [y/N] ").strip().lower()
        if answer != "y":
            return

    print("\n[SETUP] AI Review requires a Groq API key (free at https://console.groq.com)")
    key = input("        Enter your GROQ_API_KEY: ").strip()
    if not key:
        print("[WARNING] No key entered — skipping. Set GROQ_API_KEY manually later.")
        return
    _save_api_key(key)

_HOOK_TEMPLATE = """\
#!/bin/sh
# Code Review Agent — pre-commit hook
# Auto-installed by: cra install

# Use forward slashes — backslashes break Git's sh.exe on Windows
PYTHON="{python_bin}"

# Review only staged files before the commit is created (with AI review)
"$PYTHON" -m agent.cli review --staged --ai
STATUS=$?
exit $STATUS
"""


def install_hook(repo_root: Optional[str] = None, force: bool = False) -> bool:
    """Install the pre-commit hook into the git repository.

    Args:
        repo_root: Path to the git repository root. Defaults to CWD.
        force: Overwrite an existing hook without prompting.

    Returns:
        True if installation succeeded.
    """
    root = Path(repo_root or os.getcwd())
    hooks_dir = root / ".git" / "hooks"

    if not hooks_dir.exists():
        logger.error("No .git/hooks directory found at %s", root)
        print(f"[ERROR] {root} does not appear to be a git repository.")
        return False

    hook_path = hooks_dir / "pre-commit"
    # Convert backslashes to forward slashes — Git's sh.exe on Windows
    # silently fails when the Python path contains backslashes
    python_bin = sys.executable.replace("\\", "/")

    if hook_path.exists() and not force:
        content = hook_path.read_text()
        if "Code Review Agent" in content:
            print(f"[INFO] Hook already installed at {hook_path}")
            _prompt_api_key()
            return True
        print(f"[WARNING] A pre-commit hook already exists at {hook_path}")
        answer = input("Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("[ABORTED] Hook installation cancelled.")
            return False

    hook_content = _HOOK_TEMPLATE.format(
        python_bin=python_bin,
    )
    # Write with Unix line endings — CRLF breaks sh execution on Windows/Git Bash
    hook_path.write_text(hook_content, encoding="utf-8", newline="\n")

    # Make the hook executable
    current = hook_path.stat().st_mode
    hook_path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[OK] Pre-commit hook installed at {hook_path}")

    # Ask for API key
    _prompt_api_key()

    return True


def _remove_api_key() -> None:
    """Remove GROQ_API_KEY from the system environment."""
    system = platform.system()
    if system == "Windows":
        subprocess.run(["reg", "delete", "HKCU\\Environment", "/v", "GROQ_API_KEY", "/f"],
                       capture_output=True)
        os.environ.pop("GROQ_API_KEY", None)
        print("[OK] GROQ_API_KEY removed from Windows environment variables.")
    else:
        shell = os.environ.get("SHELL", "")
        profile = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
        if profile.exists():
            lines = profile.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = [l for l in lines if "GROQ_API_KEY" not in l]
            if len(new_lines) != len(lines):
                profile.write_text("".join(new_lines), encoding="utf-8")
                print(f"[OK] GROQ_API_KEY removed from {profile}")
            else:
                print("[INFO] GROQ_API_KEY not found in shell profile.")
        os.environ.pop("GROQ_API_KEY", None)


def uninstall_hook(repo_root: Optional[str] = None) -> bool:
    """Remove the code review agent pre-commit hook if it was installed by us.

    Args:
        repo_root: Path to the git repository root. Defaults to CWD.

    Returns:
        True if the hook was removed (or was not present).
    """
    root = Path(repo_root or os.getcwd())
    hook_path = root / ".git" / "hooks" / "pre-commit"

    if not hook_path.exists():
        print("[INFO] No pre-commit hook found — nothing to remove.")
    else:
        content = hook_path.read_text()
        if "Code Review Agent" not in content:
            print("[WARNING] The existing pre-commit hook was not installed by this agent.")
            print("[SKIPPED] Remove it manually if needed.")
            return False
        hook_path.unlink()
        print(f"[OK] Pre-commit hook removed from {hook_path}")

    # Offer to remove the API key too
    if os.environ.get("GROQ_API_KEY"):
        answer = input("\nDo you also want to remove the GROQ_API_KEY from your system? [y/N] ").strip().lower()
        if answer == "y":
            _remove_api_key()
    else:
        print("[INFO] No GROQ_API_KEY found in environment — nothing to clean up.")

    print("\n[INFO] To fully remove the package run:  pip uninstall code-review-agent")
    return True
