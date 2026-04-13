"""Generate human-readable review report files and console output.

When the number of violations exceeds a configurable threshold, or when
explicitly requested, this module writes a structured Markdown report file
that developers can open in their editor to review and fix issues one by one.

Console output always uses plain human language with:
  - WHY this is a problem
  - WHERE exactly it occurs (file + line + code snippet)
  - HOW to fix it (concrete suggestion)
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agent.utils.reporter import ReviewResult, Severity, Violation

# ── Human-readable explanations per rule category ────────────────────────────

_CATEGORY_EXPLANATIONS: Dict[str, str] = {
    "security": (
        "Security vulnerabilities can be exploited by attackers to steal data, "
        "execute malicious code, or gain unauthorised access to your system."
    ),
    "secrets": (
        "Hardcoded secrets (API keys, passwords, tokens) in source code can be "
        "extracted by anyone with access to the repo, leading to account compromise."
    ),
    "error_handling": (
        "Poor error handling hides bugs, makes debugging harder, and can cause "
        "unexpected crashes in production."
    ),
    "maintainability": (
        "High complexity and poor structure make code harder to understand, "
        "test, and modify — increasing the risk of introducing new bugs."
    ),
    "style": (
        "Inconsistent coding style makes the codebase harder to read and "
        "increases cognitive load for the entire team."
    ),
    "type_safety": (
        "Missing or weak types allow bugs to slip through that the compiler "
        "or type checker could have caught automatically."
    ),
    "duplication": (
        "Duplicated code means every bug fix and feature change must be made "
        "in multiple places — if you miss one, you have a regression."
    ),
    "test_coverage": (
        "Missing tests mean you have no safety net. Any change could break "
        "existing functionality without anyone noticing until production."
    ),
    "architecture": (
        "Project structure issues make it harder for new developers to onboard "
        "and for the team to maintain the codebase as it grows."
    ),
    "performance": (
        "Performance issues can lead to slow response times, high resource "
        "usage, and poor user experience."
    ),
}

_SEVERITY_LABELS = {
    Severity.ERROR:   "🔴 ERROR",
    Severity.WARNING: "🟡 WARNING",
    Severity.INFO:    "🔵 INFO",
}

_SEVERITY_HUMAN = {
    Severity.ERROR:   "This MUST be fixed before committing. It blocks the pipeline.",
    Severity.WARNING: "This SHOULD be fixed. It won't block but degrades code quality.",
    Severity.INFO:    "This is a suggestion for improvement. Consider addressing it.",
}


def _human_explanation(v: Violation) -> str:
    """Build a multi-line human-readable explanation for a single violation."""
    parts = []
    sev_label = _SEVERITY_LABELS.get(v.severity, "ISSUE")
    sev_human = _SEVERITY_HUMAN.get(v.severity, "")

    parts.append(f"{sev_label}  [{v.rule_id}] {v.rule_name}")
    parts.append(f"")

    # WHERE
    loc = f"Line {v.line_number}" if v.line_number > 0 else "File-level"
    parts.append(f"  📍 Where: {v.file_path} → {loc}")
    if v.snippet:
        parts.append(f"  📝 Code:  {v.snippet.strip()[:150]}")
    parts.append(f"")

    # WHY
    parts.append(f"  ❓ What's wrong:")
    parts.append(f"     {v.message}")
    cat = v.category.lower() if v.category else ""
    cat_explain = _CATEGORY_EXPLANATIONS.get(cat)
    if cat_explain:
        parts.append(f"")
        parts.append(f"  💡 Why it matters:")
        parts.append(f"     {cat_explain}")
    parts.append(f"")

    # HOW
    if v.fix_suggestion:
        parts.append(f"  ✅ How to fix:")
        parts.append(f"     {v.fix_suggestion}")
    parts.append(f"")

    # Severity urgency
    parts.append(f"  ⚡ Priority: {sev_human}")

    return "\n".join(parts)


def format_console_output(result: ReviewResult, duplication_stats: Optional[object] = None) -> str:
    """Format all violations as human-readable console text."""
    if not result.violations:
        return ""

    lines = []
    by_file: Dict[str, List[Violation]] = {}
    for v in result.violations:
        by_file.setdefault(v.file_path, []).append(v)

    for file_path, violations in by_file.items():
        lines.append(f"\n{'─' * 60}")
        lines.append(f"📄  {file_path}")
        lines.append(f"{'─' * 60}")

        for v in sorted(violations, key=lambda x: x.line_number):
            lines.append("")
            lines.append(_human_explanation(v))

    # Summary
    errors = sum(1 for v in result.violations if v.severity == Severity.ERROR)
    warnings = sum(1 for v in result.violations if v.severity == Severity.WARNING)
    infos = sum(1 for v in result.violations if v.severity == Severity.INFO)

    lines.append(f"\n{'═' * 60}")
    lines.append(f"  SUMMARY: {errors} error(s), {warnings} warning(s), {infos} info(s)")
    lines.append(f"  Files scanned: {result.files_scanned}")
    lines.append(f"  Rules applied: {result.rules_applied}")
    if errors > 0:
        lines.append(f"  ⛔ COMMIT BLOCKED — fix the {errors} error(s) above")
    elif warnings > 0:
        lines.append(f"  ⚠️  Warnings found — commit allowed but please review")
    else:
        lines.append(f"  ✅ Only informational suggestions — looking good!")
    lines.append(f"{'═' * 60}")

    return "\n".join(lines)


def generate_report_file(
    result: ReviewResult,
    project_root: str,
    language: str = "",
    framework: str = "",
    output_path: Optional[str] = None,
    duplication_stats: Optional[object] = None,
) -> str:
    """Write a detailed Markdown report file for developers.

    Args:
        result: The review result with all violations.
        project_root: Path to the project root.
        output_path: Where to write. Defaults to ``<project_root>/cra-report.md``.

    Returns:
        Absolute path to the generated report file.
    """
    if output_path is None:
        output_path = str(Path(project_root) / "cra-report.md")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    lines.append(f"# Code Review Agent — Detailed Report")
    lines.append(f"")
    lines.append(f"**Generated:** {now}")
    lines.append(f"**Language:** {language or 'auto-detected'}")
    lines.append(f"**Framework:** {framework or 'none'}")
    lines.append(f"**Files scanned:** {result.files_scanned}")
    lines.append(f"**Rules applied:** {result.rules_applied}")
    lines.append(f"")

    errors = result.errors
    warnings = result.warnings
    infos = result.infos

    lines.append(f"## Quick Summary")
    lines.append(f"")
    lines.append(f"| Severity | Count | Action Required |")
    lines.append(f"|----------|-------|-----------------|")
    lines.append(f"| 🔴 Error | {len(errors)} | Must fix before commit |")
    lines.append(f"| 🟡 Warning | {len(warnings)} | Should fix for quality |")
    lines.append(f"| 🔵 Info | {len(infos)} | Optional improvements |")
    lines.append(f"| **Total** | **{len(result.violations)}** | |")
    lines.append(f"")

    # Duplication metrics
    if duplication_stats and hasattr(duplication_stats, "percentage"):
        dup_pct = duplication_stats.percentage
        dup_lines = duplication_stats.duplicated_lines
        total_lines = duplication_stats.total_lines
        status = "✅ Within threshold" if dup_pct <= 10 else "🔴 Exceeds threshold"

        lines.append(f"## 📊 Code Duplication")
        lines.append(f"")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Duplication Percentage | **{dup_pct}%** |")
        lines.append(f"| Duplicated Lines | {dup_lines} |")
        lines.append(f"| Total Code Lines | {total_lines} |")
        lines.append(f"| Status | {status} |")
        lines.append(f"")
        if dup_pct > 10:
            lines.append(f"> ⚠️ **Duplication is above 10%.** Commits will be blocked until duplication is reduced.")
            lines.append(f"> Look for CROSS001 violations below and extract shared code into common modules.")
            lines.append(f"")
        elif dup_pct > 5:
            lines.append(f"> 🟡 Duplication is moderate. Consider refactoring repeated blocks to reduce maintenance burden.")
            lines.append(f"")

    if errors:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## 🔴 Errors (Must Fix)")
        lines.append(f"")
        lines.append(f"These issues **block your commit**. Fix them before pushing.")
        lines.append(f"")
        for i, v in enumerate(errors, 1):
            lines.append(f"### {i}. [{v.rule_id}] {v.message[:100]}")
            lines.append(f"")
            loc = f"Line {v.line_number}" if v.line_number > 0 else "File-level"
            lines.append(f"- **File:** `{v.file_path}`")
            lines.append(f"- **Location:** {loc}")
            if v.snippet:
                lines.append(f"- **Code:**")
                lines.append(f"  ```")
                lines.append(f"  {v.snippet.strip()[:200]}")
                lines.append(f"  ```")
            lines.append(f"- **Why this is an error:** {v.message}")
            cat = v.category.lower() if v.category else ""
            if cat in _CATEGORY_EXPLANATIONS:
                lines.append(f"- **Impact:** {_CATEGORY_EXPLANATIONS[cat]}")
            if v.fix_suggestion:
                lines.append(f"- **How to fix:** {v.fix_suggestion}")
            lines.append(f"")

    if warnings:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## 🟡 Warnings (Should Fix)")
        lines.append(f"")
        lines.append(f"These won't block your commit but indicate quality issues.")
        lines.append(f"")
        for i, v in enumerate(warnings, 1):
            lines.append(f"### {i}. [{v.rule_id}] {v.message[:100]}")
            lines.append(f"")
            loc = f"Line {v.line_number}" if v.line_number > 0 else "File-level"
            lines.append(f"- **File:** `{v.file_path}`")
            lines.append(f"- **Location:** {loc}")
            if v.snippet:
                lines.append(f"- **Code:**")
                lines.append(f"  ```")
                lines.append(f"  {v.snippet.strip()[:200]}")
                lines.append(f"  ```")
            lines.append(f"- **Issue:** {v.message}")
            if v.fix_suggestion:
                lines.append(f"- **Suggestion:** {v.fix_suggestion}")
            lines.append(f"")

    if infos:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## 🔵 Suggestions (Optional)")
        lines.append(f"")
        for i, v in enumerate(infos, 1):
            loc = f"Line {v.line_number}" if v.line_number > 0 else "File-level"
            lines.append(f"{i}. **[{v.rule_id}]** `{v.file_path}` {loc} — {v.message[:120]}")
        lines.append(f"")

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## How to Suppress Violations")
    lines.append(f"")
    lines.append(f"Add an inline comment to suppress a specific line:")
    lines.append(f"```python")
    lines.append(f"eval('some_code')  # noqa")
    lines.append(f"eval('some_code')  # cra-ignore")
    lines.append(f"```")
    lines.append(f"```javascript")
    lines.append(f"console.log('debug'); // noqa")
    lines.append(f"console.log('debug'); // cra-ignore")
    lines.append(f"```")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"*Generated by Code Review Agent*")

    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    return output_path
