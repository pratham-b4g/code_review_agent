"""Core rule engine — dispatches regex, AST, and filename rules against files."""

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.utils.logger import get_logger
from agent.utils.reporter import ReviewResult, Severity, Violation

logger = get_logger(__name__)

# Inline suppression markers — lines containing these are excluded from violations
_SUPPRESSION_RE = re.compile(
    r"(?:#|//)\s*(?:noqa|noinspection|cra-ignore)(?:\s|$|:)", re.IGNORECASE
)


def _build_suppressed_lines(content: str) -> frozenset:
    """Return a frozenset of 1-indexed line numbers that have an inline suppression comment."""
    suppressed = set()
    for i, line in enumerate(content.splitlines(), start=1):
        if _SUPPRESSION_RE.search(line):
            suppressed.add(i)
    return frozenset(suppressed)


# AST check identifiers supported by this engine
_AST_CHECKS = frozenset(
    [
        "bare_except",
        "wildcard_import",
        "print_usage",
        "eval_exec_usage",
        "missing_type_hints",
        "snake_case_functions",
        "no_unused_imports",
        # Python SonarQube-level
        "mutable_default_args",
        "cognitive_complexity",
        "too_many_params",
        "shell_injection",
        "unsafe_deserialization",
        "empty_except_body",
        "unreachable_code",
        "is_literal_comparison",
        "unused_variables",
        "fstring_no_placeholder",
        "empty_function_body",
        "duplicate_strings_py",
        "cyclomatic_complexity",
        # JS/TS SonarQube-level
        "nested_callback_depth",
        "too_many_params_js",
        "duplicate_strings",
        "no_dangerously_set_innerhtml",
        "async_without_try_catch",
        "no_unused_imports_js",
    ]
)


class RuleEngine:
    """Applies a list of rules to a set of files and collects violations.

    Supports three rule types:
    - ``regex``: simple pattern matching against each line of source code.
    - ``ast``:   delegates to language-specific AST analyzers for deeper checks.
    - ``filename``: pattern matching against the file path/name itself.
    """

    def __init__(self, python_analyzer=None, js_analyzer=None) -> None:
        # Injected analyzers — avoids circular imports and keeps engine generic
        self._python_analyzer = python_analyzer
        self._js_analyzer = js_analyzer

    def review_files(
        self,
        files: List[str],
        rules: List[Dict[str, Any]],
        max_file_size_bytes: int = 512_000,
        exclude_paths: Optional[List[str]] = None,
        changed_lines_map: Optional[Dict[str, set]] = None,
    ) -> ReviewResult:
        """Run all rules against all provided files.

        Args:
            files: List of file paths (relative to CWD) to review.
            rules: Rule dictionaries loaded by RuleLoader.
            max_file_size_bytes: Skip files larger than this.
            exclude_paths: Path segments to skip (e.g. 'node_modules').

        Returns:
            ReviewResult containing all violations.
        """
        result = ReviewResult(rules_applied=len(rules))
        skip_segments = set(exclude_paths or [])

        for file_path in files:
            path = Path(file_path)

            # Skip excluded directories
            if any(seg in path.parts for seg in skip_segments):
                logger.debug("Skipping excluded path: %s", file_path)
                continue

            if not path.exists():
                logger.debug("File not found, skipping: %s", file_path)
                continue

            if path.stat().st_size > max_file_size_bytes:
                logger.debug("Skipping oversized file: %s", file_path)
                continue

            violations = self._review_single_file(file_path, rules)

            # Diff-only mode: only keep violations on changed lines
            if changed_lines_map is not None:
                changed = changed_lines_map.get(file_path)
                if changed is not None:
                    violations = [
                        v for v in violations
                        if v.line_number == 0 or v.line_number in changed
                    ]

            result.violations.extend(violations)
            result.files_scanned += 1

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _review_single_file(
        self, file_path: str, rules: List[Dict[str, Any]]
    ) -> List[Violation]:
        """Apply all applicable rules to a single file."""
        ext = Path(file_path).suffix.lower()
        violations: List[Violation] = []

        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Cannot read %s: %s", file_path, exc)
            return []

        # Build set of lines with inline suppression (# noqa / // noqa / # cra-ignore)
        suppressed_lines = _build_suppressed_lines(content)

        for rule in rules:
            if not rule.get("enabled", True):
                continue

            file_extensions = rule.get("file_extensions", [])
            if file_extensions and ext not in file_extensions:
                continue

            exclude_patterns = rule.get("exclude_file_patterns", [])
            if exclude_patterns:
                basename = Path(file_path).name
                if any(
                    fnmatch.fnmatch(basename, pat) or fnmatch.fnmatch(file_path, pat)
                    for pat in exclude_patterns
                ):
                    continue

            rule_type = rule.get("type", "regex").lower()

            if rule_type == "regex":
                violations.extend(self._apply_regex_rule(file_path, content, rule))
            elif rule_type == "ast":
                violations.extend(self._apply_ast_rule(file_path, content, ext, rule))
            elif rule_type == "filename":
                v = self._apply_filename_rule(file_path, rule)
                if v:
                    violations.append(v)

        # Filter out violations on suppressed lines
        if suppressed_lines:
            violations = [
                v for v in violations
                if v.line_number not in suppressed_lines
            ]

        return violations

    # -- Regex -----------------------------------------------------------

    def _apply_regex_rule(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        pattern_str = rule.get("pattern", "")
        if not pattern_str:
            return []

        flags = re.IGNORECASE if rule.get("case_insensitive") else 0
        try:
            pattern = re.compile(pattern_str, flags)
        except re.error as exc:
            logger.warning("Invalid regex in rule %s: %s", rule.get("id"), exc)
            return []

        violations: List[Violation] = []
        lines = content.splitlines()
        for line_no, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if match:
                violations.append(
                    Violation(
                        rule_id=rule["id"],
                        rule_name=rule.get("name", ""),
                        severity=Severity(rule.get("severity", "warning")),
                        file_path=file_path,
                        line_number=line_no,
                        message=rule.get("message", "Violation detected"),
                        fix_suggestion=rule.get("fix_suggestion", ""),
                        snippet=line,
                        category=rule.get("category", ""),
                    )
                )
        return violations

    # -- AST (delegated) -------------------------------------------------

    def _apply_ast_rule(
        self, file_path: str, content: str, ext: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        ast_check = rule.get("ast_check", "")

        if ext == ".py" and self._python_analyzer:
            return self._python_analyzer.run_ast_check(file_path, content, rule, ast_check)

        if ext in (".js", ".jsx", ".ts", ".tsx") and self._js_analyzer:
            return self._js_analyzer.run_ast_check(file_path, content, rule, ast_check)

        # Fallback: regex approximation for unsupported languages
        fallback_pattern = rule.get("fallback_pattern")
        if fallback_pattern:
            rule_copy = {**rule, "type": "regex", "pattern": fallback_pattern}
            return self._apply_regex_rule(file_path, content, rule_copy)

        return []

    # -- Filename --------------------------------------------------------

    def _apply_filename_rule(
        self, file_path: str, rule: Dict[str, Any]
    ) -> Optional[Violation]:
        pattern_str = rule.get("pattern", "")
        expect_match = rule.get("expect_match", True)

        if not pattern_str:
            return None

        # Normalise to forward slashes so patterns work on Windows too
        normalised_path = file_path.replace("\\", "/")
        try:
            matched = bool(re.search(pattern_str, normalised_path))
        except re.error:
            return None

        if matched != expect_match:
            return Violation(
                rule_id=rule["id"],
                rule_name=rule.get("name", ""),
                severity=Severity(rule.get("severity", "warning")),
                file_path=file_path,
                line_number=0,
                message=rule.get("message", "Filename convention violated"),
                fix_suggestion=rule.get("fix_suggestion", ""),
                category=rule.get("category", ""),
            )
        return None
