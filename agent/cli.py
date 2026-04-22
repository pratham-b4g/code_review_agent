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

    "dashboard": """
Usage: cra dashboard [OPTIONS]

Launch a web-based dashboard (like SonarQube) to browse review results.
Opens http://localhost:9090 in your browser with an interactive UI
showing violations per file, inline code highlighting, severity filters,
duplication stats, and more.

Options:
  --dir PATH        Project directory to scan (default: current directory)
  --port PORT       Server port (default: 9090)
  --lang LANG       Override language detection
  --framework FW    Override framework detection
  --no-open         Don't auto-open the browser

Examples:
  cra dashboard
  cra dashboard --dir /path/to/project
  cra dashboard --port 8080
""".strip(),

    "admin": """
Usage: cra admin [OPTIONS]

Launch the Super Admin panel for managing TLs, projects, and the system.
Only accessible with Super Admin credentials.
Opens http://localhost:9090 in your browser.

Options:
  --port PORT       Server port (default: 9090)
  --no-open         Don't auto-open the browser

Examples:
  cra admin
  cra admin --port 8080
""".strip(),

    "send-reports": """
Usage: cra send-reports [OPTIONS]

Send analytics reports to TLs via email or Microsoft Teams webhook.
Typically driven by the in-process scheduler (configure per-TL time + URL
in the dashboard Settings page); this CLI is useful for ad-hoc / cron use.

Options:
  --days DAYS       Number of days to include in report (default: 1)
  --teams           Deliver via each TL's Power Automate webhook instead of email
  --tl-email EMAIL  Only send to this TL (good for testing a single webhook)

Examples:
  cra send-reports                             # email, all TLs, yesterday
  cra send-reports --teams                     # Teams, all TLs with a webhook
  cra send-reports --teams --tl-email a@b.co   # test one TL's Teams webhook
  cra send-reports --days 7                    # weekly rollup via email

Cron setup (legacy; prefer dashboard per-TL scheduler):
  0 9 * * * cra send-reports
  30 18 * * * cra send-reports --teams
""".strip(),

    "set-teams-webhook": """
Usage: cra set-teams-webhook --email EMAIL --url URL

Quickly configure a Power Automate webhook URL for a TL.
The TL must already exist in the system (use 'cra setup' first if needed).

Options:
  --email EMAIL     TL email address (required)
  --url URL         Power Automate workflow URL (required)
  --time TIME       Report time in HH:MM format (default: 09:00)
  --timezone TZ     Timezone (default: Asia/Kolkata)
  --enable          Enable scheduled reports (default: true)

Examples:
  cra set-teams-webhook --email tl@company.com --url "https://..."
  cra set-teams-webhook --email tl@company.com --url "https://..." --time "18:30"
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
  python main.py dashboard [--dir PATH] [--port PORT]
  python main.py admin    [--port PORT]
  python main.py send-reports [--days DAYS]
  python main.py set-teams-webhook --email EMAIL --url URL

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

    # ── dashboard ────────────────────────────────────────────────────────
    elif command == "dashboard":
        from agent.dashboard.server import run_dashboard

        import os
        import subprocess as _sp
        project_dir = None
        port = 9090
        language = None
        framework = None
        branch = None
        no_open = "--no-open" in args

        for i, a in enumerate(args):
            if a == "--dir" and i + 1 < len(args):
                project_dir = args[i + 1]
            elif a == "--port" and i + 1 < len(args):
                try:
                    port = int(args[i + 1])
                except ValueError:
                    print("[ERROR] --port must be a number")
                    return 2
            elif a == "--lang" and i + 1 < len(args):
                language = args[i + 1]
            elif a == "--framework" and i + 1 < len(args):
                framework = args[i + 1]
            elif a == "--branch" and i + 1 < len(args):
                branch = args[i + 1]

        project_dir = project_dir or os.getcwd()

        # Auto-detect git branch if not specified and we're inside a git repo
        if not branch:
            try:
                _r = _sp.run(
                    ["git", "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5
                )
                if _r.returncode == 0 and _r.stdout.strip():
                    branch = _r.stdout.strip()
            except Exception:
                pass

        # Auto-detect git remote URL for project matching
        repo_url = None
        try:
            _r = _sp.run(
                ["git", "-C", project_dir, "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, timeout=5
            )
            if _r.returncode == 0 and _r.stdout.strip():
                repo_url = _r.stdout.strip()
        except Exception:
            pass

        return run_dashboard(project_dir, port=port, language=language,
                             framework=framework, no_open=no_open, mode='developer',
                             branch=branch, repo_url=repo_url)

    # ── admin ───────────────────────────────────────────────────────────
    elif command == "admin":
        from agent.dashboard.server import run_dashboard

        port = 9090
        no_open = "--no-open" in args

        for i, a in enumerate(args):
            if a == "--port" and i + 1 < len(args):
                try:
                    port = int(args[i + 1])
                except ValueError:
                    print("[ERROR] --port must be a number")
                    return 2

        return run_dashboard(project_dir=None, port=port, no_open=no_open, mode='admin')

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

    # ── send-reports (cron job for daily analytics emails / Teams) ───────
    elif command == "send-reports":
        days = 1
        use_teams = False
        target_email: Optional[str] = None
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    print("[ERROR] --days must be a number"); return 2
            elif a == "--teams":
                use_teams = True
            elif a == "--tl-email" and i + 1 < len(args):
                target_email = args[i + 1].strip().lower()

        from agent.database import DatabaseManager
        from agent.analytics import get_tracker
        from datetime import date, timedelta

        mode = "Teams (Power Automate webhooks)" if use_teams else "email"
        print(f"[INFO] Sending {days}-day analytics reports via {mode}"
              + (f" to {target_email}..." if target_email else " to all TLs..."))

        try:
            db = DatabaseManager()
            tracker = get_tracker()

            tls = db.get_all_users(role='admin')
            if target_email:
                tls = [t for t in tls if (t.get('email') or '').lower() == target_email]
            if not tls:
                print("[WARN] No TLs matched — nothing to do"); return 0

            end_date = date.today()
            start_date = end_date - timedelta(days=days)
            date_str = f"{start_date} to {end_date}" if days > 1 else str(end_date)

            reports_sent = 0

            if use_teams:
                from agent.utils import teams_notifier
                for tl in tls:
                    settings = db.get_report_settings(tl['email'])
                    url = (settings or {}).get('teams_webhook_url') or ''
                    if not url:
                        print(f"  - {tl['email']}: no webhook configured, skipping"); continue
                    # Get project-wise data with severity breakdown and productivity metrics
                    projects_data = tracker.get_project_wise_summary(
                        tl_email=tl['email'], days=days, viewer_role='super_admin'
                    )
                    # Build and send project-wise report
                    report_payload = teams_notifier.build_project_wise_report(
                        projects_data=projects_data,
                        tl_name=tl['name'],
                        tl_email=tl['email'],
                        date_label=date_str,
                        report_type="daily" if days <= 1 else "monthly"
                    )
                    result = teams_notifier.post_to_teams(url, report_payload)
                    if result.get('ok'):
                        reports_sent += 1
                        db.mark_report_sent(tl['email'], end_date)
                        print(f"  ✓ Sent to {tl['email']} (HTTP {result.get('status')})")
                    else:
                        print(f"  ✗ Failed {tl['email']}: HTTP {result.get('status')} — "
                              f"{result.get('body')}")
            else:
                from agent.utils.email_notifier import get_notifier
                notifier = get_notifier()
                for tl in tls:
                    summary = tracker.get_analytics_summary(days=days)
                    if summary.get('total_commits', 0) > 0 or summary.get('developers'):
                        ok = notifier.send_daily_analytics_report(
                            tl_email=tl['email'], tl_name=tl['name'],
                            date=date_str, summary=summary,
                            developer_stats=summary.get('developers', []),
                        )
                        if ok:
                            reports_sent += 1; print(f"  ✓ Sent report to {tl['email']}")
                        else:
                            print(f"  ✗ Failed to send report to {tl['email']}")
                    else:
                        print(f"  - No activity to report for {tl['email']}")

            print(f"[INFO] Sent {reports_sent} reports"); return 0

        except Exception as e:
            print(f"[ERROR] Failed to send reports: {e}")
            return 1

    # ── set-teams-webhook (quickly configure TL webhook) ────────────────
    elif command == "set-teams-webhook":
        email: Optional[str] = None
        url: Optional[str] = None
        report_time = "09:00"
        timezone = "Asia/Kolkata"
        enabled = True

        for i, a in enumerate(args):
            if a == "--email" and i + 1 < len(args):
                email = args[i + 1].strip().lower()
            elif a == "--url" and i + 1 < len(args):
                url = args[i + 1].strip()
            elif a == "--time" and i + 1 < len(args):
                report_time = args[i + 1].strip()
            elif a == "--timezone" and i + 1 < len(args):
                timezone = args[i + 1].strip()
            elif a == "--enable":
                enabled = True
            elif a == "--disable":
                enabled = False

        if not email or not url:
            print("[ERROR] --email and --url are required")
            print(_COMMAND_HELP.get("set-teams-webhook", ""))
            return 2

        from agent.database import DatabaseManager
        try:
            db = DatabaseManager()
            user = db.get_user_by_email(email)
            if not user:
                print(f"[ERROR] TL with email '{email}' not found. Run 'cra setup' first.")
                return 1

            db.update_report_settings(
                email=email,
                teams_webhook_url=url,
                report_time=report_time,
                report_timezone=timezone,
                report_enabled=enabled,
            )
            status = "enabled" if enabled else "disabled"
            print(f"[OK] Teams webhook configured for {email}")
            print(f"     URL: {url[:60]}...")
            print(f"     Schedule: {report_time} ({timezone}) — {status}")
            print(f"\nTest it: cra send-reports --teams --tl-email {email}")
            return 0
        except Exception as e:
            print(f"[ERROR] Failed to save settings: {e}")
            return 1

    else:
        print(f"[ERROR] Unknown command '{command}'. Run with --help for usage.")
        return 2


def main_entry() -> None:
    """Console script entry point (used by pip install)."""
    sys.exit(run_cli())


if __name__ == "__main__":
    main_entry()
