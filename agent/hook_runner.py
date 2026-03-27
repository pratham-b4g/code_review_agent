"""Orchestrates a full review run — used by both the CLI and the git hook."""

import os
import sys
from typing import List, Optional

from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer
from agent.analyzer.python_analyzer import PythonAnalyzer
from agent.detector.language_detector import LanguageDetector
from agent.detector.project_context import build_project_context, group_files_by_subproject
from agent.git.git_utils import collect_files_for_push, get_repo_root, get_staged_files, scan_directory
from agent.rules.api_fetcher import ApiFetcher
from agent.rules.rule_engine import RuleEngine
from agent.rules.rule_loader import RuleLoader
from agent.linter.lint_runner import run_linting
from agent.utils.config_manager import ConfigManager
from agent.utils.logger import get_logger, set_global_log_level
from agent.utils.reporter import Reporter, ReviewResult

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
        rules = loader.load_rules(language=ctx.language, framework=ctx.framework)
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
                print("\n[BLOCKED] Fix linting errors before the rules check runs.")
                final_code = 1
                continue

        # ── Build engine with language-specific analyzers ────────────────
        py_analyzer = PythonAnalyzer()
        js_analyzer = JavaScriptAnalyzer()
        engine = RuleEngine(python_analyzer=py_analyzer, js_analyzer=js_analyzer)

        # ── Run review ───────────────────────────────────────────────────
        print(f"[INFO] Evaluating {len(subproject_files)} file(s):")
        for f in subproject_files:
            print(f"       → {f}")

        result: ReviewResult = engine.review_files(
            files=subproject_files,
            rules=rules,
            max_file_size_bytes=config.max_file_size_bytes,
            exclude_paths=config.exclude_paths,
        )

        reporter.print_result(result, block_on_warning=config.block_on_warning)

        if ai_review:
            from agent.ai.ai_reviewer import run_ai_review
            ai_code = run_ai_review(
                files=subproject_files,
                project_root=subproject_root,
                language=ctx.language,
                framework=ctx.framework,
                api_key=config.get("ai_api_key"),
                model=config.get("ai_model", "claude-haiku-4-5-20251001"),
            )
            if ai_code != 0:
                final_code = 1

        if result.has_blocking_issues(config.block_on_warning):
            final_code = 1

    return final_code


def run_as_hook() -> None:
    """Entry point called directly by the git pre-push hook script."""
    exit_code = run_review()
    sys.exit(exit_code)
