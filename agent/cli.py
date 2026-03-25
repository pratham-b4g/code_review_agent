"""CLI entry point for the Code Review Agent.

Commands:
    review  — run a review against specified files (or auto-detect)
    hook    — run as a git pre-push hook (reads push refs from stdin)
    install — install the pre-push hook into the current git repository
    uninstall — remove the hook
"""

import sys
from typing import List, Optional


def _print_usage() -> None:
    print(
        """
Code Review Agent — intelligent git pre-commit gate

Usage:
  python main.py review   [--staged] [--lang LANG] [--framework FW]
                          [--config PATH] [FILE ...]
  python main.py hook     (called automatically by git pre-push hook)
  python main.py install  [--force] [--repo PATH]
  python main.py uninstall [--repo PATH]
  python main.py rules    [--lang LANG] [--framework FW]

Options:
  --staged          Review only git-staged files (pre-commit mode)
  --lang LANG       Override language detection (python|javascript|typescript)
  --framework FW    Override framework detection (react|nextjs|fastapi|django|...)
  --config PATH     Path to .code-review-agent.yaml config file
  --force           Force-overwrite existing hook (for install)
  --repo PATH       Path to git repository root (defaults to CWD)
  FILE ...          Explicit file list (overrides git-based collection)

Exit codes:
  0 — no blocking violations
  1 — blocking violations found (push should be blocked)
  2 — usage or configuration error
""".strip()
    )


def run_cli(argv: Optional[List[str]] = None) -> int:
    """Parse argv and dispatch to the appropriate sub-command.

    Args:
        argv: Argument list. Defaults to sys.argv[1:].

    Returns:
        Exit code (0, 1, or 2).
    """
    args = list(argv if argv is not None else sys.argv[1:])

    if not args or args[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0

    command = args.pop(0)

    # ── review ──────────────────────────────────────────────────────────
    if command == "review":
        from agent.hook_runner import run_review

        staged = False
        language: Optional[str] = None
        framework: Optional[str] = None
        config_path: Optional[str] = None
        project_dir: Optional[str] = None
        files: List[str] = []

        i = 0
        while i < len(args):
            token = args[i]
            if token == "--staged":
                staged = True
            elif token == "--lang" and i + 1 < len(args):
                i += 1
                language = args[i]
            elif token == "--framework" and i + 1 < len(args):
                i += 1
                framework = args[i]
            elif token == "--config" and i + 1 < len(args):
                i += 1
                config_path = args[i]
            elif token == "--dir" and i + 1 < len(args):
                i += 1
                project_dir = args[i]
            elif not token.startswith("-"):
                files.append(token)
            i += 1

        return run_review(
            files=files or None,
            project_root=project_dir,
            language_override=language,
            framework_override=framework,
            config_path=config_path,
            staged_only=staged,
            manual_review=True,
        )

    # ── hook ────────────────────────────────────────────────────────────
    elif command == "hook":
        from agent.hook_runner import run_review
        return run_review()

    # ── install ─────────────────────────────────────────────────────────
    elif command == "install":
        from agent.git.hook_installer import install_hook

        force = "--force" in args
        repo = None
        for i, a in enumerate(args):
            if a == "--repo" and i + 1 < len(args):
                repo = args[i + 1]
                break

        success = install_hook(repo_root=repo, force=force)
        return 0 if success else 2

    # ── uninstall ────────────────────────────────────────────────────────
    elif command == "uninstall":
        from agent.git.hook_installer import uninstall_hook

        repo = None
        for i, a in enumerate(args):
            if a == "--repo" and i + 1 < len(args):
                repo = args[i + 1]
                break

        success = uninstall_hook(repo_root=repo)
        return 0 if success else 2

    # ── rules ────────────────────────────────────────────────────────────
    elif command == "rules":
        from agent.rules.rule_loader import RuleLoader

        language = "python"
        framework = None
        for i, a in enumerate(args):
            if a == "--lang" and i + 1 < len(args):
                language = args[i + 1]
            elif a == "--framework" and i + 1 < len(args):
                framework = args[i + 1]

        loader = RuleLoader()
        rules = loader.load_rules(language=language, framework=framework)
        print(f"\nLoaded {len(rules)} rules for language='{language}' framework='{framework}'\n")
        for r in rules:
            sev = r.get("severity", "?").upper()
            print(f"  [{sev:7}] {r['id']:12} {r.get('name', '')}")
        print()
        return 0

    else:
        print(f"[ERROR] Unknown command '{command}'. Run with --help for usage.")
        return 2


def main_entry() -> None:
    """Console script entry point (used by pip install)."""
    sys.exit(run_cli())


if __name__ == "__main__":
    main_entry()
