"""CLI entry point for the Code Review Agent.

Commands:
    review  — run a review against specified files (or auto-detect)
    hook    — run as a git pre-push hook (reads push refs from stdin)
    install — install the pre-push hook into the current git repository
    uninstall — remove the hook
"""

import sys
from typing import Dict, List, Optional


# ── Per-command help texts ────────────────────────────────────────────────────

_COMMAND_HELP: Dict[str, str] = {
    "review": """
Usage: python main.py review [OPTIONS] [FILE ...]

Run a full code quality review on your project or specific files.

Options:
  --dir PATH        Project directory to review (default: current directory)
  --staged          Review only git-staged files (pre-commit mode)
  --diff-only       Only flag violations on changed lines (new code focus)
  --fix             Auto-fix lint errors (ruff --fix / eslint --fix) before review
  --unsafe-fixes    Also apply ruff's unsafe fixes (use with --fix)
  --report          Force generate a detailed Markdown report (cra-report.md)
  --lang LANG       Override language detection (python|javascript|typescript)
  --framework FW    Override framework detection (react|nextjs|fastapi|django|...)
  --config PATH     Path to .code-review-agent.yaml config file
  --ai              Enable AI-powered review (requires API key)
  --skip-lint       Skip the linting step
  FILE ...          Explicit file list (overrides auto-detection)

Examples:
  python main.py review --dir /path/to/project
  python main.py review --fix --diff-only
  python main.py review --staged --report
  python main.py review src/main.py src/utils.py
""".strip(),

    "fix": """
Usage: python main.py fix [OPTIONS] [FILE ...]

Auto-fix common lint errors without running a full review.
For Python: runs ruff check --fix + ruff format.
For JavaScript/TypeScript: runs eslint --fix.

Options:
  --dir PATH        Project directory (default: current directory)
  --lang LANG       Override language detection (python|javascript|typescript)
  --unsafe-fixes    Also apply ruff's unsafe fixes (removes unused imports, etc.)
  FILE ...          Explicit file list (overrides auto-detection)

What gets auto-fixed:
  Python (ruff):    F401 unused imports, I001 import sorting, W291/W293 whitespace,
                    UP007/UP035 type annotation modernization, and 400+ more rules
  JS/TS (eslint):   Formatting, unused vars, semicolons, etc.

Examples:
  python main.py fix --dir /path/to/project
  python main.py fix --unsafe-fixes
  python main.py fix src/main.py src/utils.py
""".strip(),

    "install": """
Usage: python main.py install [OPTIONS]

Install the Code Review Agent as a git pre-push hook.
After installation, every 'git push' will automatically run the review.

Options:
  --repo PATH       Path to git repository root (default: current directory)
  --force           Overwrite existing pre-push hook if present

Examples:
  python main.py install
  python main.py install --repo /path/to/repo --force
""".strip(),

    "uninstall": """
Usage: python main.py uninstall [OPTIONS]

Remove the Code Review Agent pre-push hook from a git repository.

Options:
  --repo PATH       Path to git repository root (default: current directory)

Examples:
  python main.py uninstall
  python main.py uninstall --repo /path/to/repo
""".strip(),

    "rules": """
Usage: python main.py rules [OPTIONS]

List all available review rules for a given language/framework.

Options:
  --lang LANG       Language to show rules for (default: python)
  --framework FW    Also include framework-specific rules

Examples:
  python main.py rules --lang python
  python main.py rules --lang javascript
  python main.py rules --lang typescript --framework react
""".strip(),

    "baseline": """
Usage: python main.py baseline save [OPTIONS]

Save current violations as a baseline. Future reviews will only flag NEW
violations not present in the baseline. Useful for managing technical debt.

Options:
  --dir PATH        Project directory (default: current directory)

After saving, add to .code-review-agent.yaml:
  use_baseline: true

Baseline is stored per-branch in .cra-baseline/<branch>.json

Examples:
  python main.py baseline save
  python main.py baseline save --dir /path/to/project
""".strip(),

    "report": """
Usage: python main.py report [OPTIONS]

Generate a detailed Markdown report (cra-report.md) for the project.
Includes: severity breakdown, duplication %, code snippets, fix guidance.

Options:
  --dir PATH        Project directory (default: current directory)
  --lang LANG       Override language detection

Examples:
  python main.py report
  python main.py report --dir /path/to/project --lang python
""".strip(),

    "hook": """
Usage: python main.py hook

Run as a git pre-push hook. This is called automatically by git —
you should not need to run this manually.
Use 'python main.py install' to set up the hook.
""".strip(),

    "setup-key": """
Usage: python main.py setup-key

Interactively configure your API key for AI-powered reviews.
The key is stored locally and never committed to git.
""".strip(),

    "setup": """
Usage: python main.py setup

Interactive setup wizard for team leads to configure the project
on the remote server (team settings, rule preferences, etc.).
""".strip(),
}


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
  python main.py fix     [--dir PATH] [--lang LANG] [--unsafe-fixes] [FILE ...]
  python main.py baseline save [--dir PATH]
  python main.py report   [--dir PATH] [--lang LANG]

Options:
  --staged          Review only git-staged files (pre-commit mode)
  --diff-only       Only flag violations on changed lines (new code focus)
  --fix             Auto-fix lint errors before running the review
  --unsafe-fixes    Also apply ruff's unsafe fixes (use with --fix)
  --report          Force generate a detailed report file (cra-report.md)
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

    # Per-command --help
    if "-h" in args or "--help" in args:
        if command in _COMMAND_HELP:
            print(f"\n{_COMMAND_HELP[command]}\n")
        else:
            print(f"\nNo detailed help for '{command}'. Run 'python main.py --help' for all commands.\n")
        return 0

    # ── review ──────────────────────────────────────────────────────────
    if command == "review":
        from agent.hook_runner import run_review

        staged = False
        ai_review = False
        skip_lint = False
        diff_only = False
        force_report = False
        auto_fix = False
        unsafe_fixes = False
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
            elif token == "--ai":
                ai_review = True
            elif token == "--skip-lint":
                skip_lint = True
            elif token == "--diff-only":
                diff_only = True
            elif token == "--report":
                force_report = True
            elif token == "--fix":
                auto_fix = True
            elif token == "--unsafe-fixes":
                unsafe_fixes = True
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

        # Set flags via env so hook_runner picks them up
        import os
        if diff_only:
            os.environ["CRA_DIFF_ONLY"] = "1"
        if force_report:
            os.environ["CRA_FORCE_REPORT"] = "1"
        if auto_fix:
            os.environ["CRA_AUTO_FIX"] = "1"
        if unsafe_fixes:
            os.environ["CRA_UNSAFE_FIXES"] = "1"

        return run_review(
            files=files or None,
            project_root=project_dir,
            language_override=language,
            framework_override=framework,
            config_path=config_path,
            staged_only=staged,
            manual_review=True,
            ai_review=ai_review,
            skip_lint=skip_lint,
        )

    # ── fix ──────────────────────────────────────────────────────────────
    elif command == "fix":
        from agent.linter.lint_runner import run_autofix
        from agent.detector.language_detector import LanguageDetector
        from agent.git.git_utils import scan_directory
        from agent.utils.config_manager import ConfigManager
        import os

        language: Optional[str] = None
        project_dir: Optional[str] = None
        unsafe_fixes = "--unsafe-fixes" in args
        files: List[str] = []

        for i, a in enumerate(args):
            if a == "--lang" and i + 1 < len(args):
                language = args[i + 1]
            elif a == "--dir" and i + 1 < len(args):
                project_dir = args[i + 1]
            elif not a.startswith("-"):
                files.append(a)

        project_dir = project_dir or os.getcwd()
        config = ConfigManager()
        lang = language or LanguageDetector(project_dir).detect_primary_language()

        if not files:
            files = scan_directory(project_dir, lang, list(config.exclude_paths))

        if not files:
            print("[INFO] No files found to fix.")
            return 0

        return run_autofix(
            files=files,
            language=lang,
            project_root=project_dir,
            unsafe_fixes=unsafe_fixes,
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

    # ── baseline ─────────────────────────────────────────────────────────
    elif command == "baseline":
        sub = args[0] if args else ""
        if sub != "save":
            print("[ERROR] Usage: python main.py baseline save [--dir PATH]")
            return 2

        from agent.baseline import save_baseline
        from agent.hook_runner import run_review

        project_dir = None
        for i, a in enumerate(args):
            if a == "--dir" and i + 1 < len(args):
                project_dir = args[i + 1]

        import os
        project_dir = project_dir or os.getcwd()

        # Run a review to get current violations, then persist
        from agent.utils.config_manager import ConfigManager
        from agent.rules.rule_loader import RuleLoader
        from agent.rules.rule_engine import RuleEngine
        from agent.analyzer.python_analyzer import PythonAnalyzer
        from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer
        from agent.detector.language_detector import LanguageDetector
        from agent.git.git_utils import scan_directory

        config = ConfigManager()
        lang = LanguageDetector(project_dir).detect_primary_language()
        files = scan_directory(project_dir, lang, list(config.exclude_paths))
        loader = RuleLoader(rules_dir=config.rules_dir)
        rules = loader.load_rules(language=lang, framework=None)
        engine = RuleEngine(python_analyzer=PythonAnalyzer(), js_analyzer=JavaScriptAnalyzer())
        result = engine.review_files(files, rules, config.max_file_size_bytes, config.exclude_paths)
        path = save_baseline(project_dir, result.violations)
        print(f"[INFO] Baseline saved with {len(result.violations)} violation(s) to {path}")
        print(f"[INFO] Set 'use_baseline: true' in .code-review-agent.yaml to enable baseline filtering.")
        return 0

    # ── report ───────────────────────────────────────────────────────────
    elif command == "report":
        from agent.utils.report_generator import generate_report_file
        from agent.hook_runner import run_review as _run_review
        from agent.utils.config_manager import ConfigManager
        from agent.rules.rule_loader import RuleLoader
        from agent.rules.rule_engine import RuleEngine
        from agent.analyzer.python_analyzer import PythonAnalyzer
        from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer
        from agent.detector.language_detector import LanguageDetector
        from agent.git.git_utils import scan_directory

        import os
        project_dir = None
        language = None
        for i, a in enumerate(args):
            if a == "--dir" and i + 1 < len(args):
                project_dir = args[i + 1]
            elif a == "--lang" and i + 1 < len(args):
                language = args[i + 1]

        project_dir = project_dir or os.getcwd()
        config = ConfigManager()
        lang = language or LanguageDetector(project_dir).detect_primary_language()
        files = scan_directory(project_dir, lang, list(config.exclude_paths))
        loader = RuleLoader(rules_dir=config.rules_dir)
        rules = loader.load_rules(language=lang, framework=None)
        engine = RuleEngine(python_analyzer=PythonAnalyzer(), js_analyzer=JavaScriptAnalyzer())
        result = engine.review_files(files, rules, config.max_file_size_bytes, config.exclude_paths)
        result.deduplicate()
        path = generate_report_file(result, project_dir, lang)
        print(f"[INFO] Report generated with {len(result.violations)} violation(s): {path}")
        return 0

    # ── setup-key ────────────────────────────────────────────────────────
    elif command == "setup-key":
        from agent.git.hook_installer import _prompt_api_key
        _prompt_api_key()
        return 0

    # ── setup (TL creates project on server) ─────────────────────────────
    elif command == "setup":
        from agent.git.hook_installer import prompt_tl_setup
        prompt_tl_setup()
        return 0

    else:
        print(f"[ERROR] Unknown command '{command}'. Run with --help for usage.")
        return 2


def main_entry() -> None:
    """Console script entry point (used by pip install)."""
    sys.exit(run_cli())


if __name__ == "__main__":
    main_entry()
