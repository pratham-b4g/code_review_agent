"""Heuristic analyzer for JavaScript and TypeScript files.

Tree-sitter would provide true AST parsing but requires a compiled
native extension. This analyzer uses carefully constructed regex
patterns combined with structural heuristics to approximate many of
the same checks, keeping the agent dependency-free.
"""

import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from agent.analyzer.base_analyzer import BaseAnalyzer
from agent.utils.logger import get_logger
from agent.utils.reporter import Severity, Violation

logger = get_logger(__name__)


def _make_violation(
    rule: Dict[str, Any],
    file_path: str,
    line_no: int,
    message_override: Optional[str] = None,
    snippet: str = "",
) -> Violation:
    return Violation(
        rule_id=rule["id"],
        rule_name=rule.get("name", ""),
        severity=Severity(rule.get("severity", "warning")),
        file_path=file_path,
        line_number=line_no,
        message=message_override or rule.get("message", ""),
        fix_suggestion=rule.get("fix_suggestion", ""),
        snippet=snippet,
        category=rule.get("category", ""),
    )


def _find_pattern_lines(
    pattern: str, content: str, flags: int = 0
) -> List[Tuple[int, str]]:
    """Return (line_no, line_text) for each line matching the pattern."""
    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        return []
    results: List[Tuple[int, str]] = []
    for i, line in enumerate(content.splitlines(), start=1):
        if compiled.search(line):
            results.append((i, line))
    return results


class JavaScriptAnalyzer(BaseAnalyzer):
    """Heuristic/regex-based analyzer for JS and TS source files."""

    _CHECK_DISPATCH = {
        "no_class_components": "_check_class_components",
        "no_console_log": "_check_console_log",
        "no_var_declaration": "_check_var_declaration",
        "no_inline_styles": "_check_inline_styles",
        "no_async_storage_secrets": "_check_async_storage_secrets",
        "no_jwt_in_localstorage": "_check_jwt_in_localstorage",
        "no_any_type": "_check_any_type",
        "use_flatlist": "_check_use_flatlist",
        "no_raw_anchor": "_check_raw_anchor",
        # ── SonarQube-level checks ──
        "nested_callback_depth": "_check_nested_callback_depth",
        "too_many_params_js": "_check_too_many_params",
        "duplicate_strings": "_check_duplicate_strings",
        "no_dangerously_set_innerhtml": "_check_dangerously_set_innerhtml",
        "async_without_try_catch": "_check_async_without_try_catch",
        "no_unused_imports_js": "_check_unused_imports",
    }

    def run_ast_check(
        self,
        file_path: str,
        content: str,
        rule: Dict[str, Any],
        ast_check: str,
    ) -> List[Violation]:
        method_name = self._CHECK_DISPATCH.get(ast_check)
        if not method_name:
            logger.debug("Unknown JS check '%s'", ast_check)
            return []
        method = getattr(self, method_name)
        return method(file_path, content, rule)

    # ------------------------------------------------------------------
    # Check implementations
    # ------------------------------------------------------------------

    def _check_class_components(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect React class components — use functional components instead."""
        pattern = r"class\s+\w+\s+extends\s+(React\.Component|Component|PureComponent)"
        violations = []
        for line_no, snippet in _find_pattern_lines(pattern, content):
            violations.append(
                _make_violation(
                    rule, file_path, line_no,
                    message_override="Class component detected. Use functional components with hooks instead.",
                    snippet=snippet,
                )
            )
        return violations

    def _check_console_log(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect console.log/warn/error calls."""
        # Skip lines that are comments
        pattern = r"(?<!//\s)console\.(log|warn|error|debug|info)\s*\("
        violations = []
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if re.search(pattern, line):
                violations.append(
                    _make_violation(
                        rule, file_path, i,
                        message_override="Remove console statements before pushing. Use a logger instead.",
                        snippet=line,
                    )
                )
        return violations

    def _check_var_declaration(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Flag 'var' declarations — use const/let."""
        pattern = r"\bvar\s+\w+"
        violations = []
        for line_no, snippet in _find_pattern_lines(pattern, content):
            stripped = snippet.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            violations.append(
                _make_violation(
                    rule, file_path, line_no,
                    message_override="'var' is function-scoped and error-prone. Use 'const' or 'let' instead.",
                    snippet=snippet,
                )
            )
        return violations

    def _check_inline_styles(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect inline style objects in JSX / React Native."""
        # Match style={{ ... }} or style={styles.inline_object}
        pattern = r'style=\{\s*\{'
        violations = []
        for line_no, snippet in _find_pattern_lines(pattern, content):
            violations.append(
                _make_violation(
                    rule, file_path, line_no,
                    message_override="Avoid inline style objects. Use StyleSheet.create() or CSS Modules.",
                    snippet=snippet,
                )
            )
        return violations

    def _check_async_storage_secrets(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect AsyncStorage usage for tokens/secrets (React Native)."""
        # Sensitive keys commonly stored in AsyncStorage
        pattern = (
            r"AsyncStorage\.(setItem|getItem)\s*\(\s*['\"]"
            r"(token|jwt|auth|password|secret|credential|key|api_key)"
        )
        violations = []
        for line_no, snippet in _find_pattern_lines(pattern, content, re.IGNORECASE):
            violations.append(
                _make_violation(
                    rule, file_path, line_no,
                    message_override="Do not store secrets/tokens in AsyncStorage (insecure). "
                                     "Use react-native-keychain or expo-secure-store.",
                    snippet=snippet,
                )
            )
        return violations

    def _check_jwt_in_localstorage(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect JWT/token storage in localStorage or sessionStorage."""
        pattern = (
            r"(localStorage|sessionStorage)\.(setItem|getItem)\s*\(\s*['\"]"
            r"(token|jwt|auth|access_token|refresh_token)"
        )
        violations = []
        for line_no, snippet in _find_pattern_lines(pattern, content, re.IGNORECASE):
            violations.append(
                _make_violation(
                    rule, file_path, line_no,
                    message_override="Never store JWTs in localStorage/sessionStorage (XSS risk). "
                                     "Use HttpOnly cookies instead.",
                    snippet=snippet,
                )
            )
        return violations

    def _check_any_type(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Flag explicit 'any' type annotations in TypeScript."""
        pattern = r":\s*any\b"
        violations = []
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if re.search(pattern, line):
                violations.append(
                    _make_violation(
                        rule, file_path, i,
                        message_override="Avoid 'any' type — provide an explicit type or use 'unknown' with a type guard.",
                        snippet=line,
                    )
                )
        return violations

    def _check_use_flatlist(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Warn when .map() is used to render long lists instead of FlatList."""
        # Heuristic: .map( inside JSX return with no FlatList in the same file
        if "FlatList" in content or "SectionList" in content or "VirtualizedList" in content:
            return []  # Already using the correct component
        pattern = r"\{\s*\w+\.(map)\s*\("
        violations = []
        for line_no, snippet in _find_pattern_lines(pattern, content):
            violations.append(
                _make_violation(
                    rule, file_path, line_no,
                    message_override="Use FlatList/SectionList instead of .map() for large or dynamic lists "
                                     "to enable virtualization and better performance.",
                    snippet=snippet,
                )
            )
        return violations

    def _check_raw_anchor(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect raw <a href> used for internal navigation (use Link instead)."""
        # Flag <a href="/..."> or <a href="..." that are internal paths
        pattern = r'<a\s[^>]*href=["\']/'
        violations = []
        for line_no, snippet in _find_pattern_lines(pattern, content):
            violations.append(
                _make_violation(
                    rule, file_path, line_no,
                    message_override="Use <Link> (React Router / Next.js) instead of raw <a> for internal navigation.",
                    snippet=snippet,
                )
            )
        return violations

    # ── SonarQube-level checks ────────────────────────────────────────

    def _check_nested_callback_depth(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect deeply nested callbacks / arrow functions (callback hell)."""
        max_depth = rule.get("threshold", 4)
        violations = []
        # Track nesting by counting open braces / arrow functions in scope
        depth = 0
        # Heuristic: track {, }, and arrow/function to estimate nesting
        callback_re = re.compile(r'(=>\s*\{|function\s*\(|\bif\s*\(|\bfor\s*\(|\bwhile\s*\()')
        open_re = re.compile(r'\{')
        close_re = re.compile(r'\}')
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('*'):
                continue
            depth += len(open_re.findall(line)) - len(close_re.findall(line))
            if depth > max_depth and callback_re.search(line):
                violations.append(
                    _make_violation(
                        rule, file_path, i,
                        message_override=(
                            f"Nesting depth {depth} exceeds threshold {max_depth}. "
                            "Extract nested logic into separate functions."
                        ),
                        snippet=line,
                    )
                )
        return violations

    def _check_too_many_params(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Flag functions/arrow functions with too many parameters."""
        max_params = rule.get("threshold", 5)
        violations = []
        # Match: function name(a, b, c, d, e, f) or (a, b, c, d, e, f) =>
        pattern = re.compile(
            r'(?:function\s+\w+|(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?)?'
            r'\(([^)]{10,})\)\s*(?:=>|\{)',
        )
        for i, line in enumerate(content.splitlines(), start=1):
            m = pattern.search(line)
            if m:
                params = [p.strip() for p in m.group(1).split(',') if p.strip()]
                # Filter out destructured objects counted as one param
                if len(params) > max_params:
                    violations.append(
                        _make_violation(
                            rule, file_path, i,
                            message_override=(
                                f"Function has {len(params)} parameters "
                                f"(max {max_params}). Use an options/config object instead."
                            ),
                            snippet=line,
                        )
                    )
        return violations

    def _check_duplicate_strings(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Flag string literals that appear 3+ times (extract to constant)."""
        threshold = rule.get("threshold", 3)
        violations = []
        # Find all string literals >= 6 chars (skip short ones like 'id', 'a')
        string_re = re.compile(r'''['"]([^'"\n]{6,})['"]''')
        all_strings: List[Tuple[str, int]] = []
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('import '):
                continue
            for m in string_re.finditer(line):
                all_strings.append((m.group(1), i))

        counts = Counter(s for s, _ in all_strings)
        reported: set = set()
        for s, line_no in all_strings:
            if counts[s] >= threshold and s not in reported:
                reported.add(s)
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=(
                            f"String '{s[:40]}' is duplicated {counts[s]} times. "
                            "Extract to a named constant."
                        ),
                        snippet=content.splitlines()[line_no - 1] if line_no <= len(content.splitlines()) else "",
                    )
                )
        return violations

    def _check_dangerously_set_innerhtml(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Detect dangerouslySetInnerHTML usage (XSS security hotspot)."""
        pattern = r'dangerouslySetInnerHTML'
        violations = []
        for i, line in enumerate(content.splitlines(), start=1):
            if pattern in line:
                stripped = line.strip()
                if stripped.startswith('//') or stripped.startswith('*'):
                    continue
                violations.append(
                    _make_violation(
                        rule, file_path, i,
                        message_override=(
                            "dangerouslySetInnerHTML is an XSS risk. "
                            "Sanitize HTML with DOMPurify before rendering."
                        ),
                        snippet=line,
                    )
                )
        return violations

    def _check_async_without_try_catch(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Flag async functions that have no try/catch (unhandled promise rejection)."""
        violations = []
        # Find async function blocks and check if they contain try
        async_re = re.compile(r'async\s+(?:function\s+\w+|\w+\s*=\s*async)\s*\([^)]*\)\s*\{')
        lines = content.splitlines()
        for i, line in enumerate(lines, start=1):
            if async_re.search(line):
                # Look ahead in the function body for 'try {'
                # Simple heuristic: scan next 50 lines for 'try'
                block = '\n'.join(lines[i:min(i + 50, len(lines))])
                if 'try' not in block and '.catch' not in block:
                    violations.append(
                        _make_violation(
                            rule, file_path, i,
                            message_override=(
                                "Async function without try/catch. "
                                "Wrap in try/catch or add .catch() to handle rejections."
                            ),
                            snippet=line,
                        )
                    )
        return violations

    def _check_unused_imports(
        self, file_path: str, content: str, rule: Dict[str, Any]
    ) -> List[Violation]:
        """Heuristic check for unused ES module imports in JS/TS files."""
        violations = []
        import_re = re.compile(
            r'import\s+(?:'
            r'(?:\{\s*([^}]+)\s*\})'
            r'|([A-Za-z_$][\w$]*)'
            r'|(?:([A-Za-z_$][\w$]*)\s*,\s*\{\s*([^}]+)\s*\})'
            r')\s+from\s+[\'"]'
        )
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith('import '):
                continue
            m = import_re.search(line)
            if not m:
                continue
            names: List[str] = []
            # Named imports { a, b as c }
            for group_idx in (0, 3):
                group = m.group(group_idx + 1)
                if group:
                    for part in group.split(','):
                        part = part.strip()
                        if ' as ' in part:
                            part = part.split(' as ')[-1].strip()
                        if part:
                            names.append(part)
            # Default import
            if m.group(2):
                names.append(m.group(2))
            if m.group(3):
                names.append(m.group(3))
            # Check if each name is used anywhere else in the file
            rest_of_file = content[:content.find(line)] + content[content.find(line) + len(line):]
            for name in names:
                if not name or name == 'type':
                    continue
                # Use word-boundary search to find usage
                usage = re.search(r'\b' + re.escape(name) + r'\b', rest_of_file)
                if not usage:
                    violations.append(
                        _make_violation(
                            rule, file_path, i,
                            message_override=(
                                f"Import '{name}' is never used. Remove the unused import."
                            ),
                            snippet=line,
                        )
                    )
        return violations
