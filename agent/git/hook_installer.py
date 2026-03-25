"""Installs the code review agent as a git pre-commit hook."""

import os
import stat
import sys
from pathlib import Path
from typing import Optional

from agent.utils.logger import get_logger

logger = get_logger(__name__)

_HOOK_TEMPLATE = """\
#!/bin/sh
# Code Review Agent — pre-commit hook
# Auto-installed by: cra install

set -e

PYTHON="{python_bin}"

# Review only staged files before the commit is created
"$PYTHON" -m agent.cli review --staged
exit $?
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
    python_bin = sys.executable

    if hook_path.exists() and not force:
        content = hook_path.read_text()
        if "Code Review Agent" in content:
            print(f"[INFO] Hook already installed at {hook_path}")
            return True
        print(f"[WARNING] A pre-commit hook already exists at {hook_path}")
        answer = input("Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("[ABORTED] Hook installation cancelled.")
            return False

    hook_content = _HOOK_TEMPLATE.format(
        python_bin=python_bin,
    )
    hook_path.write_text(hook_content, encoding="utf-8")

    # Make the hook executable
    current = hook_path.stat().st_mode
    hook_path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[OK] Pre-commit hook installed at {hook_path}")
    return True


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
        return True

    content = hook_path.read_text()
    if "Code Review Agent" not in content:
        print("[WARNING] The existing pre-commit hook was not installed by this agent.")
        print("[SKIPPED] Remove it manually if needed.")
        return False

    hook_path.unlink()
    print(f"[OK] Pre-commit hook removed from {hook_path}")
    return True
