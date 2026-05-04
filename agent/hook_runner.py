"""Orchestrates a full review run — used by both the CLI and the git hook."""

import os
import sys
from pathlib import Path
from typing import List, Optional

from agent.baseline import filter_new_violations, load_baseline
from agent.analyzer.taint_analyzer import run_taint_analysis
from agent.analyzer.cross_file_analyzer import (
    detect_architecture_issues,
    detect_cross_file_duplicates,
    detect_cross_file_constants,
    detect_missing_test_files,
)
from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer
from agent.analyzer.python_analyzer import PythonAnalyzer
from agent.detector.language_detector import LanguageDetector
from agent.detector.project_context import build_project_context, group_files_by_subproject
from agent.git.git_utils import (
    collect_files_for_push,
    get_changed_lines,
    get_repo_root,
    get_staged_files,
    scan_directory,
)
from agent.rules.api_fetcher import ApiFetcher
from agent.rules.rule_engine import RuleEngine
from agent.rules.rule_loader import RuleLoader
from agent.linter.lint_runner import run_linting
from agent.utils.config_manager import ConfigManager
from agent.utils.logger import get_logger, set_global_log_level
from agent.utils.report_generator import generate_report_file
from agent.utils.reporter import Reporter, ReviewResult, Severity, Violation

logger = get_logger(__name__)


def run_review(
    files: Optional[List[str]] = None,
    project_root: Optional[str] = None,
    language_override: Optional[str] = None,
    framework_override: Optional[str] = None,
    config_path: Optional[str] = None,
    staged_only: bool = False,
    manual_review: bool = False,
    ai_review: bool = False,
    skip_lint: bool = False,
) -> int:
    """Execute a full code review and return an exit code.

    Args:
        files: Explicit list of files to review. If None, files are collected
               from git (pre-push refs or staged index).
        project_root: Repository root. Defaults to git's toplevel.
        language_override: Skip language auto-detection.
        framework_override: Skip framework auto-detection.
        config_path: Path to a .code-review-agent.yaml config file.
        staged_only: If True, review staged files regardless of push refs.

    Returns:
        0 — no blocking violations (push allowed).
        1 — blocking violations found (push blocked).
        2 — internal error.
    """
    config = ConfigManager(config_path)
    set_global_log_level(config.get("log_level", "WARNING"))

    # ── Resolve project root ────────────────────────────────────────────
    root = project_root or get_repo_root() or os.getcwd()

    # ── Collect files to review ─────────────────────────────────────────
    if files is not None:
        review_files = files
    elif staged_only:
        review_files = get_staged_files(cwd=root)
        print(f"[INFO] Staged files: {len(review_files)} file(s)")
    elif manual_review:
        # Auto-detect language from project root, then scan for matching files
        lang = language_override or LanguageDetector(root).detect_primary_language()
        review_files = scan_directory(root, lang, list(config.exclude_paths))
        print(f"[INFO] Detected language: {lang} — auto-scanned {len(review_files)} file(s) in {root}")
    else:
        review_files = collect_files_for_push()

    if not review_files:
        print("[INFO] No files to review.")
        return 0

    # Normalize all paths to forward slashes — prevents Windows backslash
    # issues from affecting rule matching, linting, and AI review downstream
    review_files = [f.replace("\\", "/") for f in review_files]

    # Filter excluded paths
    excluded = config.exclude_paths
    review_files = [
        f for f in review_files
        if not any(excl in f.replace("\\", "/").split("/") for excl in excluded)
    ]

    if not review_files:
        print("[INFO] All files are in excluded paths — nothing to review.")
        return 0

    # ── Group files by subproject (handles monorepos with client/ + server/) ──
    groups = group_files_by_subproject(root, review_files)
    if len(groups) > 1:
        print(f"[INFO] Multiple subprojects detected: {', '.join(Path(k).name for k in groups)}")

    final_code = 0

    for subproject_root, subproject_files in groups.items():
        if len(groups) > 1:
            print(f"\n{'═' * 62}")
            print(f"  Subproject: {Path(subproject_root).name}/  ({len(subproject_files)} file(s))")
            print(f"{'═' * 62}")

        # ── Detect project context ──────────────────────────────────────
        ctx = build_project_context(
            project_root=subproject_root,
            files_to_review=subproject_files,
            language_override=language_override,
            framework_override=framework_override,
        )

        print(f"[INFO] Language  : {ctx.language}")
        print(f"[INFO] Framework : {ctx.framework or 'none detected'}")

        reporter = Reporter(use_color=config.get("use_color", True))
        reporter.print_header(language=ctx.language, framework=ctx.framework or "")

        # ── Load rules ──────────────────────────────────────────────────
        loader = RuleLoader(rules_dir=config.rules_dir)
        severity_overrides = config.get("severity_overrides", {}) or {}
        rules = loader.load_rules(
            language=ctx.language,
            framework=ctx.framework,
            severity_overrides=severity_overrides,
        )
        print(f"[INFO] Rules loaded: {len(rules)} rule(s)")

        # Optionally merge rules from a remote API
        remote_url = config.remote_rules_url
        if remote_url:
            fetcher = ApiFetcher(base_url=remote_url, token=config.remote_rules_token)
            remote_rules = fetcher.fetch_rules(language=ctx.language, framework=ctx.framework)
            if remote_rules:
                existing_ids = {r.get("id") for r in rules}
                for r in remote_rules:
                    if r.get("id") not in existing_ids:
                        rules.append(r)
                logger.info("Merged %d remote rules", len(remote_rules))

        if not rules:
            print(f"[WARNING] No rules found for language='{ctx.language}' framework='{ctx.framework}'.")
            continue

        # ── Auto-fix if requested ─────────────────────────────────────────
        auto_fix = os.environ.get("CRA_AUTO_FIX") == "1"
        if auto_fix and not skip_lint:
            from agent.linter.lint_runner import run_autofix
            unsafe = os.environ.get("CRA_UNSAFE_FIXES") == "1"
            run_autofix(
                files=subproject_files,
                language=ctx.language,
                project_root=subproject_root,
                framework=ctx.framework,
                python_linter=config.get("python_linter", "auto"),
                unsafe_fixes=unsafe,
            )

        # ── Run linting first ────────────────────────────────────────────
        if not skip_lint and config.get("run_linting", True):
            lint_code = run_linting(
                files=subproject_files,
                language=ctx.language,
                project_root=subproject_root,
                framework=ctx.framework,
                python_linter=config.get("python_linter", "auto"),
                js_linter=config.get("js_linter", "eslint"),
            )
            if lint_code != 0:
                if auto_fix:
                    print("\n[WARNING] Some lint errors could not be auto-fixed. Continuing with review...")
                else:
                    print("\n[BLOCKED] Fix linting errors before the rules check runs.")
                    print("          TIP: Run 'python main.py fix --dir <project>' or add --fix to auto-fix first.")
                    final_code = 1
                    continue

        # ── Build engine with language-specific analyzers ────────────────
        py_analyzer = PythonAnalyzer()
        js_analyzer = JavaScriptAnalyzer()
        engine = RuleEngine(python_analyzer=py_analyzer, js_analyzer=js_analyzer)

        # ── Build changed-lines map for diff-only mode ─────────────────
        diff_only = config.get("diff_only", False) or os.environ.get("CRA_DIFF_ONLY") == "1"
        changed_lines_map = None
        if diff_only:
            changed_lines_map = {}
            for f in subproject_files:
                cl = get_changed_lines(f, cwd=subproject_root)
                if cl is not None:
                    changed_lines_map[f] = cl
                # None → new file, check everything (don't add to map)
            print(f"[INFO] Diff-only mode: flagging only changed lines")

        # ── Run review ───────────────────────────────────────────────────
        print(f"[INFO] Evaluating {len(subproject_files)} file(s):")
        for f in subproject_files:
            print(f"       → {f}")

        result: ReviewResult = engine.review_files(
            files=subproject_files,
            rules=rules,
            max_file_size_bytes=config.max_file_size_bytes,
            exclude_paths=config.exclude_paths,
            changed_lines_map=changed_lines_map,
        )

        # ── Cross-file analysis ─────────────────────────────────────
        cross_violations: list = []

        # 1. Cross-file duplicate detection
        dup_violations, dup_stats = detect_cross_file_duplicates(
            files=subproject_files, language=ctx.language,
        )
        cross_violations.extend(dup_violations)

        # Show duplication percentage
        dup_pct = dup_stats.percentage
        print(f"\n  📊 Code Duplication: {dup_pct}% ({dup_stats.duplicated_lines} duplicated lines / {dup_stats.total_lines} total lines)")

        # Block if duplication exceeds threshold
        max_dup_pct = float(config.get("max_duplication_percent", 10))
        if dup_pct > max_dup_pct:
            result.violations.append(
                Violation(
                    rule_id="DUP_GATE",
                    rule_name="duplication_threshold_exceeded",
                    severity=Severity.ERROR,
                    file_path=subproject_root,
                    line_number=0,
                    message=(
                        f"Code duplication is {dup_pct}% which exceeds the allowed threshold of {max_dup_pct}%. "
                        f"Reduce duplication by extracting shared logic into utility modules."
                    ),
                    fix_suggestion=(
                        f"Identify the {len(dup_violations)} duplicate block(s) listed above and refactor "
                        f"them into shared helper functions in a utils/ or common/ module."
                    ),
                    category="duplication",
                )
            )
            print(f"  🔴 BLOCKED: Duplication {dup_pct}% exceeds max allowed {max_dup_pct}%")
        elif dup_pct > 0:
            print(f"  ✅ Duplication within threshold ({max_dup_pct}% max)")

        # 2. Cross-file duplicate constants
        const_violations = detect_cross_file_constants(
            files=subproject_files, language=ctx.language,
        )
        cross_violations.extend(const_violations)

        # 3. Missing test file detection
        test_violations = detect_missing_test_files(
            files=subproject_files,
            project_root=subproject_root,
            language=ctx.language,
        )
        cross_violations.extend(test_violations)

        # 3. Architecture / structure suggestions
        arch_violations = detect_architecture_issues(
            project_root=subproject_root,
            language=ctx.language,
            framework=ctx.framework,
            files=subproject_files,
        )
        cross_violations.extend(arch_violations)

        # 4. Taint / data-flow analysis (Python only)
        if ctx.language == "python":
            for f in subproject_files:
                if f.endswith(".py"):
                    try:
                        src = Path(f).read_text(encoding="utf-8", errors="replace")
                        taint_v = run_taint_analysis(f, src)
                        cross_violations.extend(taint_v)
                    except OSError:
                        pass

        # Merge cross-file violations into the main result
        result.violations.extend(cross_violations)

        # Remove duplicate violations (same file + line + rule_id)
        result.deduplicate()

        # ── Baseline filtering (ignore known/existing violations) ──────
        use_baseline = config.get("use_baseline", False)
        if use_baseline:
            baseline_keys = load_baseline(subproject_root)
            if baseline_keys:
                result.violations, suppressed = filter_new_violations(
                    result.violations, baseline_keys,
                )
                if suppressed:
                    print(f"[INFO] Baseline: {suppressed} known violation(s) suppressed, {len(result.violations)} new issue(s)")

        reporter.print_result(result, block_on_warning=config.block_on_warning)

        # ── Report file generation ─────────────────────────────────────
        report_threshold = int(config.get("report_file_threshold", 15))
        force_report = os.environ.get("CRA_FORCE_REPORT") == "1"
        total_violations = len(result.violations)
        if total_violations > report_threshold or (force_report and total_violations > 0):
            report_path = generate_report_file(
                result=result,
                project_root=subproject_root,
                language=ctx.language,
                framework=ctx.framework or "",
                duplication_stats=dup_stats,
            )
            print(
                f"\n  📋 {total_violations} violations exceed threshold ({report_threshold})."
                f"\n     Detailed report saved to: {report_path}"
                f"\n     Open it in your editor to review and fix issues one by one.\n"
            )

        # Auto-enable AI when an API key is available in the environment,
        # even if --ai wasn't passed explicitly (e.g. running from git hook).
        _ai_keys = ["GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
        if not ai_review and any(os.environ.get(k) for k in _ai_keys):
            ai_review = True
            print("[CRA] API key detected — AI review enabled automatically.")

        ai_issues: list = []
        if ai_review:
            from agent.ai.ai_reviewer import run_ai_review
            ai_code, ai_issues = run_ai_review(
                files=subproject_files,
                project_root=subproject_root,
                language=ctx.language,
                framework=ctx.framework,
                api_key=config.get("ai_api_key"),
                model=config.get("ai_model", "claude-haiku-4-5-20251001"),
            )
            if ai_code != 0:
                final_code = 1

        blocked = result.has_blocking_issues(config.block_on_warning)
        if blocked:
            final_code = 1

        # ── Build critical issues list (error-severity rules + high/medium AI issues) ──
        rule_critical_issues = [
            {
                "source": "rules",
                "severity": v.severity.value if hasattr(v.severity, "value") else str(v.severity),
                "file": v.file_path,
                "line": v.line_number,
                "rule_id": v.rule_id,
                "category": v.category,
                "message": v.message,
            }
            for v in result.violations
            if str(getattr(v.severity, "value", v.severity)) == "error"
        ]
        critical_issues = rule_critical_issues + ai_issues

        # ── Silently report to CRA server ────────────────────────────────
        _post_review_to_server(
            language=ctx.language,
            framework=ctx.framework or "",
            result=result,
            blocked=blocked,
            repo_root=root,
            critical_issues=critical_issues,
        )

    return final_code


def _post_review_to_server(
    language: str,
    framework: str,
    result: "ReviewResult",
    blocked: bool,
    repo_root: Optional[str] = None,
    critical_issues: Optional[list] = None,
) -> None:
    """Save review to local SQLite store and trigger daily report if due.
    Never raises — a store failure must never block a commit.
    """
    try:
        from agent.git.hook_installer import load_cra_config
        from agent.local_store import save_review, check_and_send_report

        cra_cfg = load_cra_config(repo_root=repo_root)
        project_key     = cra_cfg.get("project_key", "").strip()
        developer_email = cra_cfg.get("developer_email", "").strip()

        print(f"[CRA] Config loaded — project_key={'SET' if project_key else 'MISSING'}, email={'SET' if developer_email else 'MISSING'}")
        if not project_key or not developer_email:
            print(f"[CRA] Skipping DB save — run 'cra install' in this project to link it.")
            return

        # Categorize violations by type
        security_issues = 0
        quality_issues = 0
        style_issues = 0
        performance_issues = 0

        for v in result.violations:
            category = v.category.lower()
            if category in ("security", "secrets", "authentication", "authorization"):
                security_issues += 1
            elif category in ("quality", "correctness", "maintainability", "error_handling"):
                quality_issues += 1
            elif category in ("style", "naming", "formatting", "convention"):
                style_issues += 1
            elif category in ("performance", "optimization", "efficiency"):
                performance_issues += 1
            else:
                quality_issues += 1

        save_review(
            developer_email=developer_email,
            project_key=project_key,
            language=language,
            framework=framework or "",
            quality_score=None,  # rule-only review has no numeric score
            high_issues=len([v for v in result.violations if str(getattr(v.severity, "value", v.severity)) == "error"]),
            medium_issues=len([v for v in result.violations if str(getattr(v.severity, "value", v.severity)) == "warning"]),
            low_issues=len([v for v in result.violations if str(getattr(v.severity, "value", v.severity)) not in ("error", "warning")]),
            blocked=blocked,
            files_reviewed=result.files_scanned,
            security_issues=security_issues,
            quality_issues=quality_issues,
            style_issues=style_issues,
            performance_issues=performance_issues,
            critical_issues=(critical_issues or [])[:20],
        )

        # Trigger daily report if it is past 6:30 PM IST and not yet sent today
        check_and_send_report(project_key, developer_email)

        # ── Also save to PostgreSQL so the admin panel shows the same violations ──
        # AI issues (high/medium) are already in critical_issues; combine with
        # all rule violations so the admin sees the full picture without re-calling AI.
        _save_scan_to_postgres(
            project_key=project_key,
            developer_email=developer_email,
            repo_root=repo_root,
            result=result,
            critical_issues=critical_issues or [],
        )

    except Exception:
        pass  # never block a commit because of a store failure


def _save_scan_to_postgres(
    project_key: str,
    developer_email: str,
    repo_root: Optional[str],
    result: "ReviewResult",
    critical_issues: list,
) -> None:
    """Persist the full scan result (rule + AI violations) to PostgreSQL.

    Called from _post_review_to_server after the local SQLite save, so the
    admin panel always reflects what the developer sees in the terminal
    without needing to re-run the AI reviewer on every admin Review click.
    """
    try:
        import subprocess as _sp
        from agent.database import DatabaseManager

        print(f"[CRA] Saving scan to PostgreSQL (project_key={project_key}, email={developer_email})")
        db = DatabaseManager()
        project = db.get_project_by_key(project_key)
        if not project:
            print(f"[CRA] Project not found in DB for key={project_key} — skipping PostgreSQL save")
            return

        project_id = project["id"]

        # Detect current branch
        branch = "main"
        if repo_root:
            br = _sp.run(
                ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if br.returncode == 0 and br.stdout.strip():
                branch = br.stdout.strip()

        print(f"[CRA] Project id={project_id} branch={branch}")

        # Build unified violation list: all rule violations + AI violations
        # AI issues come via critical_issues (source=="ai"); rule violations
        # from result.violations cover all severities (error/warning/info).
        _ai_sev_map = {"high": "error", "medium": "warning", "low": "info"}
        violations: list = []

        for v in result.violations:
            sev = v.severity.value if hasattr(v.severity, "value") else str(v.severity)
            fp = (v.file_path or "").replace("\\", "/")
            violations.append({
                "file": fp,
                "line": v.line_number,
                "severity": sev,
                "message": v.message,
                "rule_id": v.rule_id,
                "category": v.category,
            })

        # Separate AI violations into dedicated column (accumulated, never overwritten)
        ai_violations = []
        for issue in critical_issues:
            if issue.get("source") != "ai":
                continue
            raw_sev = issue.get("severity", "medium")
            ai_violations.append({
                "source": "ai",
                "file": (issue.get("file") or "").replace("\\", "/"),
                "line": issue.get("line") or 0,
                "severity": _ai_sev_map.get(raw_sev, "warning"),
                "message": issue.get("message", ""),
                "rule_id": issue.get("category") or "AI",
                "category": issue.get("category", ""),
            })

        print(f"[CRA] Saving {len(violations)} rule violations + {len(ai_violations)} AI violations to DB")

        errors = len([v for v in violations if v["severity"] == "error"])
        warnings = len([v for v in violations if v["severity"] == "warning"])
        total = len(violations)
        quality_score = max(0.0, 100.0 - errors * 5 - warnings * 2)

        # Save rule violations (overwrites per subproject run — that's fine)
        db.save_project_scan(
            project_id=project_id,
            branch=branch,
            scanned_by_email=developer_email,
            violations=violations,
            quality_score=quality_score,
        )
        # Accumulate AI violations separately — never lost across subprojects or admin rescans
        if ai_violations:
            db.save_ai_violations(project_id=project_id, branch=branch, ai_violations=ai_violations)

        print(f"[CRA] Saved to PostgreSQL: {total} rule + {len(ai_violations)} AI violations")
    except Exception as _e:
        print(f"[CRA] PostgreSQL save failed (non-blocking): {_e}")


def run_as_hook() -> None:
    """Entry point called directly by the git pre-push hook script."""
    exit_code = run_review()
    
    # Track analytics for the push
    try:
        track_push_analytics()
    except Exception:
        pass  # Don't block push if analytics fails
    
    sys.exit(exit_code)


def track_push_analytics():
    """Track git push analytics for the dashboard."""
    import subprocess
    from agent.database.db_manager import DatabaseManager
    from agent.analytics.tracker import AnalyticsTracker
    
    # Get git info
    try:
        # Get repo path
        result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], 
                               capture_output=True, text=True, check=True)
        repo_path = result.stdout.strip()
        
        # Get user email from git config
        result = subprocess.run(['git', 'config', 'user.email'],
                               capture_output=True, text=True, check=True)
        user_email = result.stdout.strip()
        
        # Get current branch
        result = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                               capture_output=True, text=True, cwd=repo_path, check=True)
        branch = result.stdout.strip() or 'main'
        
        # Get commits being pushed
        result = subprocess.run(['git', 'log', 'HEAD@{1}..HEAD', '--oneline'],
                               capture_output=True, text=True, cwd=repo_path)
        commit_count = len([l for l in result.stdout.strip().split('\n') if l])
        
        if commit_count == 0:
            return
        
        # Initialize DB and find project by project_key (canonical lookup)
        db = DatabaseManager()

        from agent.git.hook_installer import load_cra_config
        cra_cfg = load_cra_config(repo_root=repo_path)
        project_key = cra_cfg.get("project_key", "").strip()

        project_id = None
        if project_key:
            project = db.get_project_by_key(project_key)
            if project:
                project_id = project["id"]

        # Fallback: path matching for repos not yet using project_key
        if not project_id:
            for p in db.get_all_projects():
                if repo_path in p.get("path", "") or p.get("path", "") in repo_path:
                    project_id = p["id"]
                    break

        if not project_id:
            return  # Project not registered in dashboard
        
        # Check if user is assigned to this project
        user_projects = db.get_user_projects(user_email)
        if not any(up['id'] == project_id for up in user_projects):
            return  # User not assigned to this project
        
        # Log the activity with branch
        from datetime import date
        db.log_analytics(
            user_email=user_email,
            project_id=project_id,
            date=date.today(),
            branch=branch,
            commits_count=commit_count,
            issues_found=0,  # Will be updated by full scan
            code_quality_score=100,
            effort_score=commit_count * 10
        )
        
    except Exception as e:
        print(f"[Analytics] Failed to track: {e}")
