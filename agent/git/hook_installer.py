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


_PROVIDERS = {
    "1": ("GROQ_API_KEY",       "Groq",      "https://console.groq.com        (Free, no limits)"),
    "2": ("GEMINI_API_KEY",     "Gemini",    "https://aistudio.google.com     (Free tier)"),
    "3": ("OPENAI_API_KEY",     "OpenAI",    "https://platform.openai.com     (Paid)"),
    "4": ("ANTHROPIC_API_KEY",  "Anthropic", "https://console.anthropic.com   (Paid)"),
}


def _save_api_key(env_var: str, key: str) -> None:
    """Persist the given API key as a permanent system environment variable."""
    system = platform.system()
    if system == "Windows":
        subprocess.run(["setx", env_var, key], check=True, capture_output=True)
        os.environ[env_var] = key
        print(f"[OK] {env_var} saved to Windows environment variables.")
        print("")
        print("=" * 62)
        print("  IMPORTANT: Close this terminal and open a NEW one before")
        print("  running git commit — Windows requires a new session to")
        print("  load the saved environment variable.")
        print("=" * 62)
    else:
        shell = os.environ.get("SHELL", "")
        profile = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
        line = f'\nexport {env_var}="{key}"\n'
        with open(profile, "a", encoding="utf-8") as f:
            f.write(line)
        os.environ[env_var] = key
        print(f"[OK] {env_var} saved to {profile}")
        print(f"[INFO] Run: source {profile}  (or open a new terminal)")


def _prompt_api_key() -> None:
    """Ask the user to choose an AI provider and enter their API key."""
    # Check if any key is already set
    existing_var = next(
        (var for var, _, _ in _PROVIDERS.values() if os.environ.get(var)), None
    )
    if existing_var:
        print(f"\n[INFO] {existing_var} is already set ({os.environ[existing_var][:8]}...).")
        try:
            answer = input("       Do you want to replace it? [y/N] ").strip().lower()
        except KeyboardInterrupt:
            print("\n[WARNING] Skipped.")
            return
        if answer != "y":
            return

    print("\n[SETUP] Choose your AI provider for code review:\n")
    for num, (env_var, name, url) in _PROVIDERS.items():
        print(f"  {num}. {name:12}  {url}")

    print()
    try:
        choice = input("  Enter choice (1-4): ").strip()
    except KeyboardInterrupt:
        print("\n[WARNING] Skipped. Run 'cra install' again to set the key later.")
        return

    if choice not in _PROVIDERS:
        print("[WARNING] Invalid choice — skipping. Run 'cra install' again to set the key later.")
        return

    env_var, name, url = _PROVIDERS[choice]
    print(f"\n  Get your free API key at: {url.split()[0]}")
    try:
        key = input(f"  Enter your {env_var}: ").strip()
    except KeyboardInterrupt:
        print("\n[WARNING] Skipped. Run 'cra install' again to set the key later.")
        return

    if not key:
        print("[WARNING] No key entered — skipping. Run 'cra install' again to set it later.")
        return

    _save_api_key(env_var, key)

_CRA_CONFIG = Path.home() / ".cra" / "config.json"
_REPO_PROJECT_KEY_FILE = ".git/cra_project_key"   # relative to repo root, not tracked by git


def _get_git_identity() -> tuple:
    """Read developer name and email from git global config."""
    try:
        name = subprocess.check_output(
            ["git", "config", "--global", "user.name"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        email = subprocess.check_output(
            ["git", "config", "--global", "user.email"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return name, email
    except Exception:
        return "", ""


def _save_cra_config(data: dict) -> None:
    """Save developer config to ~/.cra/config.json"""
    _CRA_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    import json
    existing = {}
    if _CRA_CONFIG.exists():
        try:
            existing = json.loads(_CRA_CONFIG.read_text())
        except Exception:
            pass
    existing.update(data)
    _CRA_CONFIG.write_text(json.dumps(existing, indent=2))


def _save_repo_project_key(repo_root: str, project_key: str) -> None:
    """Save project key into .git/cra_project_key inside the repo (not tracked by git)."""
    key_file = Path(repo_root) / _REPO_PROJECT_KEY_FILE
    key_file.write_text(project_key, encoding="utf-8")


def _load_repo_project_key(repo_root: Optional[str] = None) -> str:
    """Read project key from .git/cra_project_key for the current/given repo."""
    from agent.git.git_utils import get_repo_root
    root = repo_root or get_repo_root()
    if not root:
        return ""
    key_file = Path(root) / _REPO_PROJECT_KEY_FILE
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return ""


def load_cra_config(repo_root: Optional[str] = None) -> dict:
    """Load developer config — global fields from ~/.cra/config.json,
    project_key from .git/cra_project_key of the current repo."""
    import json
    config = {}
    if _CRA_CONFIG.exists():
        try:
            config = json.loads(_CRA_CONFIG.read_text())
        except Exception:
            pass
    # per-repo project key overrides anything stored globally
    repo_key = _load_repo_project_key(repo_root)
    if repo_key:
        config["project_key"] = repo_key
    return config


def _register_on_server(name: str, email: str, project_key: str) -> bool:
    """Confirm the project_key is valid in the local store."""
    try:
        from agent.local_store import save_developer
        result = save_developer(name, email, project_key)
        if result:
            print(f"[OK] Registered in project '{result['project']}' — TL: {result['tl']}")
            return True
        else:
            print(f"[WARNING] Project key '{project_key}' not found in local store.")
            return False
    except Exception as e:
        print(f"[WARNING] Local registration failed: {e}")
        return False


def _prompt_developer_setup(repo_root: Optional[str] = None) -> None:
    """Ask developer for project key and register on server."""
    from agent.git.git_utils import get_repo_root
    root = repo_root or get_repo_root()

    global_config = _load_global_config()
    existing_key = _load_repo_project_key(root)

    if existing_key and global_config.get("developer_email"):
        print(f"\n[INFO] This repo is already linked to project key: {existing_key}")
        print(f"[INFO] Registered as: {global_config.get('developer_name')} ({global_config.get('developer_email')})")
        try:
            answer = input("       Do you want to update registration for this repo? [y/N] ").strip().lower()
        except KeyboardInterrupt:
            return
        if answer != "y":
            return

    print("\n[SETUP] Developer Registration")
    print("─" * 40)

    # auto-detect from git
    git_name, git_email = _get_git_identity()

    if git_name and git_email:
        print(f"  Detected from git config:")
        print(f"  Name  : {git_name}")
        print(f"  Email : {git_email}")
        try:
            use_git = input("  Use these details? [Y/n] ").strip().lower()
        except KeyboardInterrupt:
            return
        if use_git in ("", "y"):
            name, email = git_name, git_email
        else:
            name, email = _ask_name_email()
    else:
        print("  Could not detect git identity. Please enter manually.")
        name, email = _ask_name_email()

    if not name or not email:
        print("[WARNING] Name/email required — skipping registration.")
        return

    # ── Auto-read project config from cra-project.json in the repo ────────────
    project_key = ""
    if root:
        import json as _json
        config_file = Path(root) / "cra-project.json"
        if config_file.exists():
            try:
                project_config = _json.loads(config_file.read_text(encoding="utf-8"))
                project_key = project_config.get("project_key", "").strip()
                if project_key:
                    # Import the project config into this developer's local DB
                    from agent.local_store import save_project_from_config
                    save_project_from_config(project_config)
                    print(f"\n  [INFO] Project config loaded from cra-project.json")
                    print(f"  Project : {project_config.get('name', '')}")
                    print(f"  TL      : {project_config.get('tl_name', '')} ({project_config.get('tl_email', '')})")
            except Exception as e:
                print(f"[WARNING] Could not read cra-project.json: {e}")

    # Fall back to manual entry if no config file found
    if not project_key:
        print("\n  [INFO] No cra-project.json found in this repo.")
        try:
            project_key = input("  Enter Project Key (get from your TL): ").strip()
        except KeyboardInterrupt:
            print("\n[WARNING] Skipped.")
            return

    if not project_key:
        print("[WARNING] Project key required — skipping registration.")
        return

    # register developer in local store
    success = _register_on_server(name, email, project_key)

    # save global fields (name, email) to ~/.cra/config.json
    _save_cra_config({
        "developer_name": name,
        "developer_email": email,
    })

    # save project_key into this repo's .git/cra_project_key
    if root:
        _save_repo_project_key(root, project_key)
        print(f"[OK] Project key saved to {root}/.git/cra_project_key")

    if success:
        print("[OK] Developer config saved to ~/.cra/config.json")


def _load_global_config() -> dict:
    """Load only the global config file, without merging repo project key."""
    import json
    if not _CRA_CONFIG.exists():
        return {}
    try:
        return json.loads(_CRA_CONFIG.read_text())
    except Exception:
        return {}


def prompt_tl_setup() -> None:
    """Interactive TL project setup — called by `cra setup` command."""
    print("\n[SETUP] Create a new CRA project (TL)\n" + "─" * 40)

    try:
        project_name = input("  Project name                                          : ").strip()
        tl_name      = input("  Your name (TL)                                        : ").strip()
        tl_email     = input("  Your email (TL)                                       : ").strip()
    except KeyboardInterrupt:
        print("\n[WARNING] Cancelled.")
        return

    if not project_name or not tl_name or not tl_email:
        print("[ERROR] All fields are required.")
        return

    try:
        from agent.local_store import save_project
        project_key = save_project(
            name=project_name,
            tl_name=tl_name,
            tl_email=tl_email,
        )

        import json as _json
        from agent.git.git_utils import get_repo_root
        repo_root = get_repo_root() or os.getcwd()
        config_file = Path(repo_root) / "cra-project.json"
        config_data = {
            "project_key": project_key,
            "name": project_name,
            "tl_name": tl_name,
            "tl_email": tl_email,
        }
        config_file.write_text(_json.dumps(config_data, indent=2), encoding="utf-8")

        print("\n" + "=" * 50)
        print(f"  Project created : {project_name}")
        print(f"  Project Key     : {project_key}")
        print(f"  Config file     : cra-project.json  (commit this to your repo)")
        print("=" * 50)
        print("\n  Next steps:")
        print("  1. Commit cra-project.json to the repo")
        print("  2. Developers run  cra install  — config is read automatically")
        print("  3. Every machine will send its own daily report to the TL at 6:30 PM IST\n")
    except Exception as e:
        print(f"[ERROR] Could not save project locally: {e}")


def _ask_name_email() -> tuple:
    """Ask developer to enter name and email manually."""
    try:
        name = input("  Your name : ").strip()
        email = input("  Your email: ").strip()
        return name, email
    except KeyboardInterrupt:
        return "", ""


_HOOK_TEMPLATE = """\
#!/bin/sh
# Code Review Agent — pre-commit hook
# Auto-installed by: cra install

# Use forward slashes — backslashes break Git's sh.exe on Windows
PYTHON="{python_bin}"

# Deactivate any active venv so the system Python (where cra is installed) is used
unset VIRTUAL_ENV
unset PYTHONHOME

# Review only staged files before the commit is created
# Add --ai to enable AI-powered deep review (requires an API key)
"$PYTHON" -m agent.cli review --staged
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

    # Register developer on CRA server (pass repo_root so project key is saved per-repo)
    _prompt_developer_setup(repo_root=str(root))

    return True


def _remove_api_key() -> None:
    """Remove all known AI provider API keys from the system environment."""
    all_vars = [var for var, _, _ in _PROVIDERS.values()]
    system = platform.system()
    removed = []

    for env_var in all_vars:
        if system == "Windows":
            result = subprocess.run(
                ["reg", "delete", "HKCU\\Environment", "/v", env_var, "/f"],
                capture_output=True,
            )
            if result.returncode == 0:
                removed.append(env_var)
        else:
            shell = os.environ.get("SHELL", "")
            profile = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
            if profile.exists():
                lines = profile.read_text(encoding="utf-8").splitlines(keepends=True)
                new_lines = [l for l in lines if env_var not in l]
                if len(new_lines) != len(lines):
                    profile.write_text("".join(new_lines), encoding="utf-8")
                    removed.append(env_var)
        os.environ.pop(env_var, None)

    if removed:
        print(f"[OK] Removed: {', '.join(removed)}")
    else:
        print("[INFO] No API keys found to remove.")


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

    # Offer to remove any set API keys
    all_vars = [var for var, _, _ in _PROVIDERS.values()]
    set_vars = [v for v in all_vars if os.environ.get(v)]
    if set_vars:
        print(f"\n[INFO] Found API key(s): {', '.join(set_vars)}")
        try:
            answer = input("Do you also want to remove them from your system? [y/N] ").strip().lower()
        except KeyboardInterrupt:
            answer = "n"
        if answer == "y":
            _remove_api_key()
    else:
        print("[INFO] No API keys found in environment — nothing to clean up.")

    print("\n[INFO] To fully remove the package run:  pip uninstall code-review-agent")
    return True
