"""Lightweight data-flow / taint analysis for Python security checks.

Traces user-controlled inputs (sources) through variable assignments to
dangerous function calls (sinks).  This is NOT a full taint engine — it
covers the most common single-file patterns that SonarQube flags:

 - HTTP request data → SQL query  (SQL injection)
 - HTTP request data → open() / os.system() / subprocess  (command injection)
 - HTTP request data → redirect() / HttpResponseRedirect  (open redirect)
 - HTTP request data → requests.get/post  (SSRF)
 - HTTP request data → render_template_string / Markup  (XSS / template injection)
"""

import ast
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.utils.logger import get_logger
from agent.utils.reporter import Severity, Violation

logger = get_logger(__name__)

# ── Source patterns: user-controllable input ──────────────────────────────────

_SOURCES: Dict[str, str] = {
    # Flask / Django / FastAPI
    "request.args": "HTTP query parameter",
    "request.form": "HTTP form data",
    "request.data": "HTTP request body",
    "request.json": "HTTP JSON body",
    "request.get_json": "HTTP JSON body",
    "request.headers": "HTTP headers",
    "request.cookies": "HTTP cookies",
    "request.files": "HTTP uploaded files",
    "request.GET": "Django query parameter",
    "request.POST": "Django form data",
    "request.body": "Django raw body",
    "request.META": "Django request metadata",
    "request.query_params": "DRF query parameters",
    # sys / os
    "sys.argv": "command-line argument",
    "os.environ": "environment variable",
    "input": "user console input",
}

# ── Sink patterns: dangerous operations ──────────────────────────────────────

_SINKS: Dict[str, Tuple[str, str]] = {
    # (category, human description)
    "cursor.execute": ("sql_injection", "SQL query execution"),
    "execute": ("sql_injection", "SQL query execution"),
    "executemany": ("sql_injection", "SQL batch execution"),
    "raw": ("sql_injection", "Django raw SQL"),
    "os.system": ("command_injection", "OS command execution"),
    "os.popen": ("command_injection", "OS command execution"),
    "subprocess.call": ("command_injection", "subprocess execution"),
    "subprocess.run": ("command_injection", "subprocess execution"),
    "subprocess.Popen": ("command_injection", "subprocess execution"),
    "redirect": ("open_redirect", "HTTP redirect"),
    "HttpResponseRedirect": ("open_redirect", "HTTP redirect"),
    "requests.get": ("ssrf", "outbound HTTP request"),
    "requests.post": ("ssrf", "outbound HTTP request"),
    "requests.put": ("ssrf", "outbound HTTP request"),
    "requests.delete": ("ssrf", "outbound HTTP request"),
    "httpx.get": ("ssrf", "outbound HTTP request"),
    "httpx.post": ("ssrf", "outbound HTTP request"),
    "urllib.request.urlopen": ("ssrf", "outbound HTTP request"),
    "render_template_string": ("xss", "template rendering with raw string"),
    "Markup": ("xss", "marking string as safe HTML"),
    "open": ("path_traversal", "file open"),
    "eval": ("code_injection", "dynamic code evaluation"),
    "exec": ("code_injection", "dynamic code execution"),
}


def _resolve_attr(node: ast.AST) -> str:
    """Recursively resolve an attribute chain: request.args.get → 'request.args.get'."""
    if isinstance(node, ast.Attribute):
        parent = _resolve_attr(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _resolve_attr(node.value)
    if isinstance(node, ast.Call):
        return _resolve_attr(node.func)
    return ""


class _TaintVisitor(ast.NodeVisitor):
    """Walk an AST, track tainted variables, and detect source→sink flows."""

    def __init__(self, lines: List[str]) -> None:
        self.tainted: Dict[str, Tuple[int, str]] = {}  # var_name → (line, source_desc)
        self.flows: List[Tuple[int, str, str, str, str]] = []  # (line, var, source_desc, sink, category)
        self._lines = lines

    # ── Track assignments ─────────────────────────────────────────────

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_taint_assignment(node.targets, node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.target and node.value:
            self._check_taint_assignment([node.target], node.value, node.lineno)
        self.generic_visit(node)

    def _check_taint_assignment(self, targets: list, value: ast.AST, lineno: int) -> None:
        """If the value is a source or derives from a tainted var, taint the target."""
        value_str = _resolve_attr(value)
        source_desc = None

        # Direct source (attribute, subscript, or call like request.args.get())
        for src, desc in _SOURCES.items():
            if value_str.startswith(src):
                source_desc = desc
                break

        # Subscript on a tainted or source: sys.argv[1], request.args['key']
        if source_desc is None and isinstance(value, ast.Subscript):
            sub_str = _resolve_attr(value.value)
            for src, desc in _SOURCES.items():
                if sub_str.startswith(src):
                    source_desc = desc
                    break

        # Propagation: value references a tainted variable
        if source_desc is None and isinstance(value, ast.Name) and value.id in self.tainted:
            source_desc = self.tainted[value.id][1]

        # BinOp propagation: 'SELECT ... ' + tainted_var
        if source_desc is None and isinstance(value, ast.BinOp):
            for operand in (value.left, value.right):
                if isinstance(operand, ast.Name) and operand.id in self.tainted:
                    source_desc = f"derived from {self.tainted[operand.id][1]}"
                    break

        # Function call returning tainted data
        if source_desc is None and isinstance(value, ast.Call):
            call_str = _resolve_attr(value.func)
            for src, desc in _SOURCES.items():
                if call_str.startswith(src):
                    source_desc = desc
                    break
            # Propagation via call: func(tainted_var)
            if source_desc is None:
                for arg in value.args:
                    if isinstance(arg, ast.Name) and arg.id in self.tainted:
                        source_desc = f"derived from {self.tainted[arg.id][1]}"
                        break

        # f-string / string concat with tainted var
        if source_desc is None and isinstance(value, ast.JoinedStr):
            for part in value.values:
                if isinstance(part, ast.FormattedValue):
                    inner = _resolve_attr(part.value) if hasattr(part, "value") else ""
                    if isinstance(part.value, ast.Name) and part.value.id in self.tainted:
                        source_desc = f"f-string with {self.tainted[part.value.id][1]}"
                        break

        if source_desc:
            for t in targets:
                if isinstance(t, ast.Name):
                    self.tainted[t.id] = (lineno, source_desc)

    # ── Detect sinks ─────────────────────────────────────────────────

    def _find_tainted_in_expr(self, node: ast.AST) -> Optional[str]:
        """Recursively find the first tainted variable name in an expression."""
        if isinstance(node, ast.Name) and node.id in self.tainted:
            return node.id
        if isinstance(node, ast.BinOp):
            left = self._find_tainted_in_expr(node.left)
            if left:
                return left
            return self._find_tainted_in_expr(node.right)
        if isinstance(node, ast.JoinedStr):
            for part in getattr(node, "values", []):
                if isinstance(part, ast.FormattedValue):
                    found = self._find_tainted_in_expr(part.value)
                    if found:
                        return found
        if isinstance(node, ast.Call):
            for arg in node.args:
                found = self._find_tainted_in_expr(arg)
                if found:
                    return found
        return None

    def visit_Call(self, node: ast.Call) -> None:
        call_str = _resolve_attr(node.func)
        sink_match = None
        for sink_name, (cat, desc) in _SINKS.items():
            if call_str.endswith(sink_name):
                sink_match = (sink_name, cat, desc)
                break

        if sink_match:
            sink_name, category, sink_desc = sink_match
            # Check if any arg is tainted
            for arg in node.args:
                tainted_var = self._find_tainted_in_expr(arg)
                if tainted_var:
                    _, src_desc = self.tainted[tainted_var]
                    self.flows.append((node.lineno, tainted_var, src_desc, sink_desc, category))

            # Also check keyword args
            for kw in node.keywords:
                tainted_var = self._find_tainted_in_expr(kw.value)
                if tainted_var:
                    _, src_desc = self.tainted[tainted_var]
                    self.flows.append((node.lineno, tainted_var, src_desc, sink_desc, category))

        self.generic_visit(node)


_CATEGORY_TO_FIX: Dict[str, str] = {
    "sql_injection": "Use parameterised queries: cursor.execute('SELECT * FROM t WHERE id = %s', (user_id,))",
    "command_injection": "Avoid passing user input to shell commands. Use subprocess with a list and shell=False.",
    "open_redirect": "Validate the redirect URL against an allowlist of safe domains.",
    "ssrf": "Validate and restrict outbound URLs to an allowlist. Never pass raw user input to HTTP clients.",
    "xss": "Use template auto-escaping. Never mark user input as safe/Markup.",
    "path_traversal": "Validate file paths. Use os.path.basename() or pathlib to strip directory traversal.",
    "code_injection": "Never use eval/exec with user input. Use ast.literal_eval() for safe parsing.",
}


def run_taint_analysis(
    file_path: str,
    content: str,
) -> List[Violation]:
    """Run lightweight taint analysis on a single Python file.

    Returns a list of Violation objects for each detected source→sink flow.
    """
    try:
        tree = ast.parse(content, filename=file_path)
    except SyntaxError:
        return []

    lines = content.splitlines()
    visitor = _TaintVisitor(lines)
    visitor.visit(tree)

    violations: List[Violation] = []
    seen: set = set()

    for line_no, var_name, source_desc, sink_desc, category in visitor.flows:
        key = (file_path, line_no, category)
        if key in seen:
            continue
        seen.add(key)

        snippet = lines[line_no - 1] if line_no <= len(lines) else ""
        violations.append(
            Violation(
                rule_id="TAINT001",
                rule_name=f"taint_{category}",
                severity=Severity.ERROR,
                file_path=file_path,
                line_number=line_no,
                message=(
                    f"Potential {category.replace('_', ' ')}: variable '{var_name}' "
                    f"originates from {source_desc} and flows into {sink_desc}. "
                    f"User-controlled data must be validated or sanitised before use."
                ),
                fix_suggestion=_CATEGORY_TO_FIX.get(category, "Validate and sanitise user input."),
                snippet=snippet,
                category="security",
            )
        )

    return violations
