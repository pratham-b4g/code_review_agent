"""AST-based analyzer for Python source files.

Uses the built-in ``ast`` module to perform deep structural checks that
simple regex patterns cannot reliably handle.
"""

import ast
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.analyzer.base_analyzer import BaseAnalyzer
from agent.utils.logger import get_logger
from agent.utils.reporter import Severity, Violation

logger = get_logger(__name__)

_SNAKE_CASE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


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


class PythonAnalyzer(BaseAnalyzer):
    """Deep Python static analysis using the standard ast module."""

    # Dispatch table: ast_check id → method name
    _CHECK_DISPATCH = {
        "bare_except": "_check_bare_except",
        "wildcard_import": "_check_wildcard_imports",
        "print_usage": "_check_print_usage",
        "eval_exec_usage": "_check_eval_exec",
        "missing_type_hints": "_check_missing_type_hints",
        "snake_case_functions": "_check_snake_case_functions",
        "no_unused_imports": "_check_unused_imports",
        # ── SonarQube-level checks ──
        "mutable_default_args": "_check_mutable_default_args",
        "cognitive_complexity": "_check_cognitive_complexity",
        "too_many_params": "_check_too_many_params",
        "shell_injection": "_check_shell_injection",
        "unsafe_deserialization": "_check_unsafe_deserialization",
        "empty_except_body": "_check_empty_except_body",
        "unreachable_code": "_check_unreachable_code",
        "is_literal_comparison": "_check_is_literal_comparison",
        "unused_variables": "_check_unused_variables",
        "fstring_no_placeholder": "_check_fstring_no_placeholder",
        "empty_function_body": "_check_empty_function_body",
        "duplicate_strings_py": "_check_duplicate_strings",
        "cyclomatic_complexity": "_check_cyclomatic_complexity",
    }

    def run_ast_check(
        self,
        file_path: str,
        content: str,
        rule: Dict[str, Any],
        ast_check: str,
    ) -> List[Violation]:
        """Dispatch to the appropriate AST check method."""
        method_name = self._CHECK_DISPATCH.get(ast_check)
        if not method_name:
            logger.debug("Unknown AST check '%s' for Python", ast_check)
            return []

        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError as exc:
            logger.warning("Syntax error parsing %s: %s", file_path, exc)
            return []

        method = getattr(self, method_name)
        lines = content.splitlines()
        return method(tree, file_path, content, lines, rule)

    # ------------------------------------------------------------------
    # AST check implementations
    # ------------------------------------------------------------------

    def _check_bare_except(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect bare ``except:`` clauses (PEP 8 / error handling guideline)."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override="Bare 'except:' catches all exceptions including system exits. "
                                         "Use 'except Exception as e:' or a specific exception type.",
                        snippet=snippet,
                    )
                )
        return violations

    def _check_wildcard_imports(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect ``from module import *`` statements."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        line_no = node.lineno
                        snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                        violations.append(
                            _make_violation(
                                rule, file_path, line_no,
                                message_override=f"Wildcard import from '{node.module}' pollutes the namespace. "
                                                 "Import only what you need.",
                                snippet=snippet,
                            )
                        )
        return violations

    def _check_print_usage(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect direct ``print()`` calls — use ``logging`` instead."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_print = (
                    (isinstance(func, ast.Name) and func.id == "print")
                    or (isinstance(func, ast.Attribute) and func.attr == "print")
                )
                if is_print:
                    line_no = node.lineno
                    snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                    violations.append(
                        _make_violation(
                            rule, file_path, line_no,
                            message_override="Use 'logging' instead of print() for backend/library code.",
                            snippet=snippet,
                        )
                    )
        return violations

    def _check_eval_exec(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect usage of ``eval()`` or ``exec()`` — security risk."""
        violations: List[Violation] = []
        dangerous = {"eval", "exec"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in dangerous:
                    line_no = node.lineno
                    snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                    violations.append(
                        _make_violation(
                            rule, file_path, line_no,
                            message_override=f"'{name}()' is a security risk. "
                                             "Never use eval/exec with untrusted input.",
                            snippet=snippet,
                        )
                    )
        return violations

    def _check_missing_type_hints(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Flag public functions/methods that are missing type annotations."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Skip private/dunder methods
            if node.name.startswith("_"):
                continue

            args = node.args
            all_params = args.args + args.posonlyargs + args.kwonlyargs
            if args.vararg:
                all_params.append(args.vararg)
            if args.kwarg:
                all_params.append(args.kwarg)

            unannotated = [
                a.arg for a in all_params
                if a.annotation is None and a.arg != "self" and a.arg != "cls"
            ]
            missing_return = node.returns is None

            if unannotated or missing_return:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                missing_parts: List[str] = []
                if unannotated:
                    missing_parts.append(f"params: {', '.join(unannotated)}")
                if missing_return:
                    missing_parts.append("return type")
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=f"Function '{node.name}' is missing type hints for {'; '.join(missing_parts)}.",
                        snippet=snippet,
                    )
                )
        return violations

    def _check_snake_case_functions(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Flag functions whose names are not snake_case (PEP 8)."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if name.startswith("__") and name.endswith("__"):
                continue  # dunder methods are exempt
            if not _SNAKE_CASE_RE.match(name):
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=f"Function '{name}' should use snake_case naming (PEP 8).",
                        snippet=snippet,
                    )
                )
        return violations

    def _check_unused_imports(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect imports that are never referenced in the module body."""
        violations: List[Violation] = []

        # Collect all imported names
        imported: Dict[str, int] = {}  # name → line number
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    imported[local_name] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname or alias.name
                    imported[local_name] = node.lineno

        # Collect all Name usages outside import statements
        used: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                # e.g. os.path — the root 'os' counts as used
                if isinstance(node.value, ast.Name):
                    used.add(node.value.id)

        for name, line_no in imported.items():
            if name not in used:
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=f"Import '{name}' is never used. Remove it to keep the code clean.",
                        snippet=snippet,
                    )
                )
        return violations

    # ── SonarQube-level checks ────────────────────────────────────────

    def _check_mutable_default_args(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect mutable default arguments (list, dict, set literals)."""
        violations: List[Violation] = []
        mutable_types = (ast.List, ast.Dict, ast.Set)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for default in node.args.defaults + node.args.kw_defaults:
                if default is not None and isinstance(default, mutable_types):
                    line_no = node.lineno
                    snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                    violations.append(
                        _make_violation(
                            rule, file_path, line_no,
                            message_override=(
                                f"Mutable default argument in '{node.name}()'. "
                                "Use None and create inside the function body."
                            ),
                            snippet=snippet,
                        )
                    )
                    break  # one violation per function is enough
        return violations

    def _check_cognitive_complexity(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Flag functions with high cognitive complexity (deep nesting + branches)."""
        max_complexity = rule.get("threshold", 15)
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            score = self._compute_cognitive_complexity(node)
            if score > max_complexity:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=(
                            f"Function '{node.name}' has cognitive complexity {score} "
                            f"(threshold {max_complexity}). Refactor to reduce nesting and branches."
                        ),
                        snippet=snippet,
                    )
                )
        return violations

    @staticmethod
    def _compute_cognitive_complexity(func_node: ast.AST, depth: int = 0) -> int:
        """Simplified cognitive complexity scorer (SonarSource-inspired)."""
        score = 0
        _BRANCH_NODES = (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With)
        _LOGIC_OPS = (ast.BoolOp,)
        for node in ast.iter_child_nodes(func_node):
            if isinstance(node, _BRANCH_NODES):
                score += 1 + depth  # nesting penalty
                score += PythonAnalyzer._compute_cognitive_complexity(node, depth + 1)
            elif isinstance(node, _LOGIC_OPS):
                score += 1
            else:
                score += PythonAnalyzer._compute_cognitive_complexity(node, depth)
        return score

    def _check_too_many_params(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Flag functions with too many parameters (default: > 5)."""
        max_params = rule.get("threshold", 5)
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            params = [a for a in args.args if a.arg not in ("self", "cls")]
            params += args.posonlyargs + args.kwonlyargs
            if len(params) > max_params:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=(
                            f"Function '{node.name}' has {len(params)} parameters "
                            f"(max {max_params}). Group into a dataclass or config object."
                        ),
                        snippet=snippet,
                    )
                )
        return violations

    def _check_shell_injection(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect subprocess calls with shell=True (command injection risk)."""
        violations: List[Violation] = []
        dangerous_funcs = {"run", "call", "Popen", "check_output", "check_call"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = None
            if isinstance(func, ast.Attribute) and func.attr in dangerous_funcs:
                func_name = func.attr
            elif isinstance(func, ast.Name) and func.id in dangerous_funcs:
                func_name = func.id
            if func_name is None:
                continue
            for kw in node.keywords:
                if kw.arg == "shell":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        line_no = node.lineno
                        snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                        violations.append(
                            _make_violation(
                                rule, file_path, line_no,
                                message_override=(
                                    f"subprocess.{func_name}() called with shell=True. "
                                    "This is a command injection risk. Use a list of args instead."
                                ),
                                snippet=snippet,
                            )
                        )
        return violations

    def _check_unsafe_deserialization(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect pickle.loads, yaml.load (without SafeLoader), marshal.loads."""
        violations: List[Violation] = []
        dangerous = {
            ("pickle", "loads"): "pickle.loads() can execute arbitrary code. Use JSON or a safe format.",
            ("pickle", "load"): "pickle.load() can execute arbitrary code. Use JSON or a safe format.",
            ("marshal", "loads"): "marshal.loads() is unsafe with untrusted data.",
            ("marshal", "load"): "marshal.load() is unsafe with untrusted data.",
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                key = (func.value.id, func.attr)
                if key in dangerous:
                    line_no = node.lineno
                    snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                    violations.append(
                        _make_violation(
                            rule, file_path, line_no,
                            message_override=dangerous[key],
                            snippet=snippet,
                        )
                    )
                # yaml.load without SafeLoader
                if key == ("yaml", "load"):
                    has_safe = any(
                        (isinstance(kw.value, ast.Attribute) and "safe" in kw.value.attr.lower())
                        or (isinstance(kw.value, ast.Name) and "safe" in kw.value.id.lower())
                        for kw in node.keywords if kw.arg == "Loader"
                    )
                    if not has_safe:
                        line_no = node.lineno
                        snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                        violations.append(
                            _make_violation(
                                rule, file_path, line_no,
                                message_override=(
                                    "yaml.load() without SafeLoader is a code execution risk. "
                                    "Use yaml.safe_load() or pass Loader=yaml.SafeLoader."
                                ),
                                snippet=snippet,
                            )
                        )
        return violations

    def _check_empty_except_body(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect except blocks that silently swallow errors (pass-only or empty)."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body
            is_empty = (
                len(body) == 1
                and isinstance(body[0], ast.Pass)
            ) or (
                len(body) == 1
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)  # docstring / Ellipsis
            )
            if is_empty:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=(
                            "Empty except body silently swallows errors. "
                            "At minimum, log the exception."
                        ),
                        snippet=snippet,
                    )
                )
        return violations

    def _check_unreachable_code(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect statements after return/raise/break/continue that are unreachable."""
        violations: List[Violation] = []
        _TERMINAL = (ast.Return, ast.Raise, ast.Break, ast.Continue)
        for node in ast.walk(tree):
            body: Optional[List] = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for i, stmt in enumerate(body):
                if isinstance(stmt, _TERMINAL) and i + 1 < len(body):
                    next_stmt = body[i + 1]
                    line_no = getattr(next_stmt, "lineno", 0)
                    snippet = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
                    violations.append(
                        _make_violation(
                            rule, file_path, line_no,
                            message_override=(
                                "Unreachable code detected after "
                                f"{type(stmt).__name__.lower()} statement."
                            ),
                            snippet=snippet,
                        )
                    )
                    break  # one per block
        return violations

    def _check_is_literal_comparison(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect 'is' comparison with a literal (int/str/float) — should use '=='."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Is, ast.IsNot)):
                    continue
                # Check if either side is a literal (not None/True/False)
                for val in [node.left, comparator]:
                    if (
                        isinstance(val, ast.Constant)
                        and not isinstance(val.value, bool)
                        and val.value is not None
                    ):
                        line_no = node.lineno
                        snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                        op_str = "is" if isinstance(op, ast.Is) else "is not"
                        violations.append(
                            _make_violation(
                                rule, file_path, line_no,
                                message_override=(
                                    f"Do not use '{op_str}' with a literal. "
                                    "Use '==' or '!=' for value comparison."
                                ),
                                snippet=snippet,
                            )
                        )
                        break
        return violations

    def _check_unused_variables(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect local variables assigned but never read within a function."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned: Dict[str, int] = {}  # name → line
            read: Set[str] = set()
            # Param names are "used" implicitly
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                read.add(arg.arg)
            if node.args.vararg:
                read.add(node.args.vararg.arg)
            if node.args.kwarg:
                read.add(node.args.kwarg.arg)

            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and not target.id.startswith("_"):
                            assigned.setdefault(target.id, child.lineno)
                elif isinstance(child, ast.AnnAssign):
                    if isinstance(child.target, ast.Name) and not child.target.id.startswith("_"):
                        assigned.setdefault(child.target.id, child.lineno)
                elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    read.add(child.id)

            for name, line_no in assigned.items():
                if name not in read:
                    snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                    violations.append(
                        _make_violation(
                            rule, file_path, line_no,
                            message_override=(
                                f"Variable '{name}' is assigned but never used. "
                                "Remove it or prefix with '_' if intentionally unused."
                            ),
                            snippet=snippet,
                        )
                    )
        return violations

    def _check_fstring_no_placeholder(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect f-strings that contain no {placeholders} — wasteful / likely a bug."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            has_expr = any(isinstance(v, ast.FormattedValue) for v in node.values)
            if not has_expr:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override="f-string has no placeholders. Use a regular string instead.",
                        snippet=snippet,
                    )
                )
        return violations

    def _check_empty_function_body(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Detect functions whose body is just 'pass' or '...' with no docstring."""
        violations: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            # Skip abstract methods and protocol stubs
            decorators = [getattr(d, 'attr', getattr(d, 'id', '')) for d in node.decorator_list]
            if 'abstractmethod' in decorators:
                continue
            is_empty = False
            if len(body) == 1:
                stmt = body[0]
                if isinstance(stmt, ast.Pass):
                    is_empty = True
                elif (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is ...
                ):
                    is_empty = True
            if is_empty:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=(
                            f"Function '{node.name}' has an empty body (only pass/...). "
                            "Implement it or remove it."
                        ),
                        snippet=snippet,
                    )
                )
        return violations

    def _check_duplicate_strings(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Flag string literals repeated 3+ times in a single Python file."""
        from collections import Counter
        threshold = rule.get("threshold", 3)
        min_length = 6
        violations: List[Violation] = []
        all_strings: List[Tuple[str, int]] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and len(node.value) >= min_length
            ):
                all_strings.append((node.value, node.lineno))

        counts = Counter(s for s, _ in all_strings)
        reported: set = set()
        for s, line_no in all_strings:
            if counts[s] >= threshold and s not in reported:
                reported.add(s)
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=(
                            f"String '{s[:40]}' is duplicated {counts[s]} times. "
                            "Extract to a module-level constant."
                        ),
                        snippet=snippet,
                    )
                )
        return violations

    # ── Cyclomatic complexity ─────────────────────────────────────────

    def _check_cyclomatic_complexity(
        self,
        tree: ast.AST,
        file_path: str,
        content: str,
        lines: List[str],
        rule: Dict[str, Any],
    ) -> List[Violation]:
        """Compute McCabe cyclomatic complexity per function.

        CC = 1 + number of decision points (if, elif, for, while, except,
        with, assert, and, or, ternary IfExp).
        """
        threshold = rule.get("threshold", 10)
        violations: List[Violation] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            cc = 1  # base path
            for child in ast.walk(node):
                if isinstance(child, (ast.If, ast.IfExp)):
                    cc += 1
                elif isinstance(child, ast.For):
                    cc += 1
                elif isinstance(child, ast.While):
                    cc += 1
                elif isinstance(child, ast.ExceptHandler):
                    cc += 1
                elif isinstance(child, ast.With):
                    cc += 1
                elif isinstance(child, ast.Assert):
                    cc += 1
                elif isinstance(child, ast.BoolOp):
                    # Each 'and' / 'or' adds len(values) - 1 paths
                    cc += len(child.values) - 1

            if cc > threshold:
                line_no = node.lineno
                snippet = lines[line_no - 1] if line_no <= len(lines) else ""
                violations.append(
                    _make_violation(
                        rule, file_path, line_no,
                        message_override=(
                            f"Function '{node.name}' has cyclomatic complexity {cc} "
                            f"(threshold {threshold}). Simplify by reducing branches."
                        ),
                        snippet=snippet,
                    )
                )
        return violations
