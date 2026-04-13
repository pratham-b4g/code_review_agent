"""Console reporter with colored output for review violations."""

import io
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

# Re-wrap stdout to UTF-8 on Windows so emoji/unicode prints without crashing
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ANSI color codes
_RED = "\033[91m"
_YELLOW = "\033[93m"
_BLUE = "\033[94m"
_GREEN = "\033[92m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

SEVERITY_COLORS: Dict[Severity, str] = {
    Severity.ERROR: _RED,
    Severity.WARNING: _YELLOW,
    Severity.INFO: _BLUE,
}

SEVERITY_ICONS: Dict[Severity, str] = {
    Severity.ERROR: "✖",
    Severity.WARNING: "⚠",
    Severity.INFO: "ℹ",
}


@dataclass
class Violation:
    """Represents a single rule violation found in a file."""

    rule_id: str
    rule_name: str
    severity: Severity
    file_path: str
    line_number: int
    message: str
    fix_suggestion: str = ""
    snippet: str = ""
    category: str = ""


@dataclass
class ReviewResult:
    """Aggregated result of a full code review run."""

    violations: List[Violation] = field(default_factory=list)
    files_scanned: int = 0
    rules_applied: int = 0

    @property
    def errors(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == Severity.WARNING]

    @property
    def infos(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == Severity.INFO]

    def has_blocking_issues(self, block_on_warning: bool = False) -> bool:
        if self.errors:
            return True
        if block_on_warning and self.warnings:
            return True
        return False

    def deduplicate(self) -> None:
        """Remove duplicate violations (same file + line + rule_id)."""
        seen: set = set()
        unique: List[Violation] = []
        for v in self.violations:
            key = (v.file_path, v.line_number, v.rule_id)
            if key not in seen:
                seen.add(key)
                unique.append(v)
        self.violations = unique


_CATEGORY_WHY: Dict[str, str] = {
    "security": "Security flaws can be exploited by attackers to steal data or execute malicious code.",
    "secrets": "Hardcoded secrets in code can be extracted by anyone with repo access.",
    "error_handling": "Poor error handling hides bugs and causes unexpected crashes in production.",
    "maintainability": "High complexity makes code harder to understand, test, and safely modify.",
    "style": "Inconsistent style increases cognitive load and slows down code reviews.",
    "type_safety": "Weak types let bugs slip through that the compiler could have caught.",
    "duplication": "Duplicated code means fixes must be applied in multiple places — easy to miss one.",
    "test_coverage": "Missing tests mean changes can break existing features without anyone noticing.",
    "architecture": "Structural issues make onboarding harder and increase maintenance cost.",
    "performance": "Performance issues cause slow responses and poor user experience.",
    "convention": "Naming/convention violations make the codebase inconsistent and harder to navigate.",
    "correctness": "This pattern is likely a bug that will cause incorrect behavior at runtime.",
    "dead_code": "Dead code clutters the codebase and confuses developers about what is actually used.",
}


class Reporter:
    """Formats and prints review results to stdout."""

    def __init__(self, use_color: bool = True):
        self.use_color = use_color and sys.stdout.isatty()

    def _c(self, text: str, *codes: str) -> str:
        """Apply ANSI color codes if color is enabled."""
        if not self.use_color:
            return text
        return "".join(codes) + text + _RESET

    def print_header(self, language: str, framework: str) -> None:
        """Print the review session header."""
        lines = [
            "",
            self._c("=" * 62, _BOLD),
            self._c("  Code Review Agent  —  Pre-Commit Gate", _BOLD),
        ]
        if language:
            lines.append(f"  Language  : {self._c(language, _CYAN)}")
        if framework:
            lines.append(f"  Framework : {self._c(framework, _CYAN)}")
        lines.append(self._c("=" * 62, _BOLD))
        lines.append("")
        print("\n".join(lines))

    def print_result(self, result: ReviewResult, block_on_warning: bool = False) -> None:
        """Print all violations with human-readable guidance."""
        if not result.violations:
            ok = (
                f"\n  {self._c('✔ All checks passed!', _GREEN, _BOLD)}"
                f"  ({result.files_scanned} file(s) scanned,"
                f" {result.rules_applied} rule(s) applied)\n"
            )
            print(ok)
            return

        # Group violations by file path for readability
        by_file: Dict[str, List[Violation]] = {}
        for v in result.violations:
            by_file.setdefault(v.file_path, []).append(v)

        for file_path, violations in by_file.items():
            print(self._c(f"\n  📄 {file_path}", _BOLD, _CYAN))
            for v in sorted(violations, key=lambda x: x.line_number):
                color = SEVERITY_COLORS.get(v.severity, "")
                icon = SEVERITY_ICONS.get(v.severity, "•")
                label = f"[{v.severity.value.upper():7}]"
                loc = f"L{v.line_number}" if v.line_number > 0 else "file"

                # ── Main violation line ──
                print(
                    f"    {self._c(icon + ' ' + label, color)}"
                    f"  {loc:>6}  {self._c(v.rule_id, _BOLD)} — {v.message}"
                )

                # ── Code snippet (what the problematic code looks like) ──
                if v.snippet:
                    print(f"             {self._c('→', _CYAN)} {v.snippet.strip()[:120]}")

                # ── Human-readable "why" explanation ──
                why = _CATEGORY_WHY.get(v.category.lower(), "") if v.category else ""
                if why:
                    print(f"             {self._c('Why:', _YELLOW)} {why}")

                # ── How to fix it ──
                if v.fix_suggestion:
                    print(f"             {self._c('Fix:', _GREEN)} {v.fix_suggestion}")

        errors = len(result.errors)
        warnings = len(result.warnings)
        total = len(result.violations)

        print(f"\n  {'─' * 58}")
        print(
            f"  {result.files_scanned} file(s) scanned  |  "
            f"{result.rules_applied} rule(s) applied  |  "
            f"{self._c(str(errors) + ' error(s)', _RED)}  "
            f"{self._c(str(warnings) + ' warning(s)', _YELLOW)}  "
            f"({total} total)"
        )

        if result.has_blocking_issues(block_on_warning):
            print(
                self._c(
                    "\n  🚫  Commit BLOCKED — fix the violations above and try again.\n",
                    _RED, _BOLD,
                )
            )
        else:
            print(
                self._c(
                    "\n  ⚠   Warning(s) found — push is allowed but consider fixing them.\n",
                    _YELLOW,
                )
            )
