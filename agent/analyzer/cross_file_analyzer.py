"""Cross-file analysis: duplication detection, missing test files, architecture checks.

These checks operate on the *set* of files being reviewed rather than on
individual files, complementing the per-file rule engine with project-wide
quality gates.
"""

import ast
import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.utils.logger import get_logger
from agent.utils.reporter import Severity, Violation

logger = get_logger(__name__)


# ── Cross-file duplicate detection ────────────────────────────────────────────

def _normalise_source(content: str) -> str:
    """Strip comments, blank lines, and normalise whitespace for comparison."""
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _extract_function_blocks_python(content: str) -> List[Tuple[str, int, str]]:
    """Return (function_name, start_line, normalised_body_hash) for each Python function."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    blocks: List[Tuple[str, int, str]] = []
    lines = content.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno - 1
        end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
        body_lines = lines[start:end]
        body_text = _normalise_source("\n".join(body_lines))
        if len(body_text) < 80:  # skip trivial functions
            continue
        body_hash = hashlib.md5(body_text.encode()).hexdigest()
        blocks.append((node.name, node.lineno, body_hash))
    return blocks


def _extract_function_blocks_js(content: str) -> List[Tuple[str, int, str]]:
    """Return (function_name, start_line, normalised_body_hash) for JS/TS functions."""
    blocks: List[Tuple[str, int, str]] = []
    # Match named functions and arrow function assignments
    func_re = re.compile(
        r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z_$]\w*)\s*=>)"
    )
    lines = content.splitlines()
    for i, line in enumerate(lines):
        m = func_re.search(line)
        if not m:
            continue
        name = m.group(1) or m.group(2) or "anonymous"
        # Grab up to 60 lines as the function body (heuristic)
        body_end = min(i + 60, len(lines))
        depth = 0
        for j in range(i, body_end):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0 and j > i:
                body_end = j + 1
                break
        body_text = _normalise_source("\n".join(lines[i:body_end]))
        if len(body_text) < 80:
            continue
        body_hash = hashlib.md5(body_text.encode()).hexdigest()
        blocks.append((name, i + 1, body_hash))
    return blocks


def _extract_code_blocks_python(content: str) -> List[Tuple[str, int, str]]:
    """Extract class methods AND compound statement blocks (if/for/while/try) as hashable blocks.

    Returns (label, start_line, body_hash) for blocks with non-trivial bodies.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    blocks: List[Tuple[str, int, str]] = []
    lines = content.splitlines()

    for node in ast.walk(tree):
        # Class methods
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Check if inside a class
            pass  # already handled by _extract_function_blocks_python

        # Compound blocks: if/for/while/try with 5+ lines
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try)):
            start = getattr(node, "lineno", 0) - 1
            end = getattr(node, "end_lineno", start + 1) if hasattr(node, "end_lineno") else start + 1
            if end - start < 5:
                continue
            body_lines = lines[start:end]
            body_text = _normalise_source("\n".join(body_lines))
            if len(body_text) < 60:
                continue
            body_hash = hashlib.md5(body_text.encode()).hexdigest()
            label = f"{type(node).__name__}@L{node.lineno}"
            blocks.append((label, node.lineno, body_hash))

    return blocks


def _extract_code_blocks_js(content: str) -> List[Tuple[str, int, str]]:
    """Extract JS code blocks by brace-delimited compound statements (5+ lines)."""
    blocks: List[Tuple[str, int, str]] = []
    lines = content.splitlines()
    # Match compound statements: if, for, while, try, switch
    compound_re = re.compile(r"^\s*(if|for|while|try|switch)\s*[\({]")
    for i, line in enumerate(lines):
        m = compound_re.search(line)
        if not m:
            continue
        depth = 0
        body_end = min(i + 80, len(lines))
        for j in range(i, body_end):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0 and j > i:
                body_end = j + 1
                break
        if body_end - i < 5:
            continue
        body_text = _normalise_source("\n".join(lines[i:body_end]))
        if len(body_text) < 60:
            continue
        body_hash = hashlib.md5(body_text.encode()).hexdigest()
        label = f"{m.group(1)}@L{i + 1}"
        blocks.append((label, i + 1, body_hash))
    return blocks


@dataclass
class DuplicationStats:
    """Holds duplication metrics for the scanned codebase."""
    total_lines: int = 0
    duplicated_lines: int = 0

    @property
    def percentage(self) -> float:
        if self.total_lines == 0:
            return 0.0
        return round((self.duplicated_lines / self.total_lines) * 100, 1)


def _count_code_lines(content: str) -> int:
    """Count non-blank, non-comment lines in a source file."""
    count = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        count += 1
    return count


def _block_line_count(content: str, start_line: int, end_line: int) -> int:
    """Count non-blank lines in a block range (1-indexed start, exclusive end)."""
    lines = content.splitlines()
    count = 0
    for i in range(max(0, start_line - 1), min(end_line, len(lines))):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            count += 1
    return count


def _extract_blocks_with_spans_python(content: str) -> List[Tuple[str, int, int, str]]:
    """Return (label, start_line, end_line, body_hash) for Python functions + compound blocks."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    blocks: List[Tuple[str, int, int, str]] = []
    lines = content.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start
            body_text = _normalise_source("\n".join(lines[start - 1:end]))
            if len(body_text) < 80:
                continue
            body_hash = hashlib.md5(body_text.encode()).hexdigest()
            blocks.append((node.name, start, end, body_hash))
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try)):
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", start) if hasattr(node, "end_lineno") else start
            if end - start < 5:
                continue
            body_text = _normalise_source("\n".join(lines[start - 1:end]))
            if len(body_text) < 60:
                continue
            body_hash = hashlib.md5(body_text.encode()).hexdigest()
            blocks.append((f"{type(node).__name__}@L{start}", start, end, body_hash))
    return blocks


def _extract_blocks_with_spans_js(content: str) -> List[Tuple[str, int, int, str]]:
    """Return (label, start_line, end_line, body_hash) for JS functions + compound blocks."""
    blocks: List[Tuple[str, int, int, str]] = []
    lines = content.splitlines()

    # Functions
    func_re = re.compile(
        r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z_$]\w*)\s*=>)"
    )
    for i, line in enumerate(lines):
        m = func_re.search(line)
        if not m:
            continue
        name = m.group(1) or m.group(2) or "anonymous"
        body_end = min(i + 60, len(lines))
        depth = 0
        for j in range(i, body_end):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0 and j > i:
                body_end = j + 1
                break
        body_text = _normalise_source("\n".join(lines[i:body_end]))
        if len(body_text) < 80:
            continue
        body_hash = hashlib.md5(body_text.encode()).hexdigest()
        blocks.append((name, i + 1, body_end, body_hash))

    # Compound blocks
    compound_re = re.compile(r"^\s*(if|for|while|try|switch)\s*[\({]")
    for i, line in enumerate(lines):
        m = compound_re.search(line)
        if not m:
            continue
        depth = 0
        body_end = min(i + 80, len(lines))
        for j in range(i, body_end):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0 and j > i:
                body_end = j + 1
                break
        if body_end - i < 5:
            continue
        body_text = _normalise_source("\n".join(lines[i:body_end]))
        if len(body_text) < 60:
            continue
        body_hash = hashlib.md5(body_text.encode()).hexdigest()
        blocks.append((f"{m.group(1)}@L{i + 1}", i + 1, body_end, body_hash))

    return blocks


def detect_cross_file_duplicates(
    files: List[str],
    language: str,
) -> Tuple[List[Violation], DuplicationStats]:
    """Find functions AND code blocks with identical normalised bodies across different files.

    Returns:
        A tuple of (violations_list, DuplicationStats) where stats include
        total code lines, duplicated lines, and the duplication percentage.
    """
    stats = DuplicationStats()

    # hash → list of (file, label, start_line, end_line, content)
    hash_map: Dict[str, List[Tuple[str, str, int, int, str]]] = defaultdict(list)
    file_contents: Dict[str, str] = {}

    for file_path in files:
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_contents[file_path] = content
        stats.total_lines += _count_code_lines(content)

        ext = Path(file_path).suffix.lower()
        if ext == ".py":
            spans = _extract_blocks_with_spans_python(content)
        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            spans = _extract_blocks_with_spans_js(content)
        else:
            continue
        for label, start, end, h in spans:
            hash_map[h].append((file_path, label, start, end, content))

    # Find duplicates and count duplicated lines
    violations: List[Violation] = []
    counted_ranges: Set[Tuple[str, int, int]] = set()  # avoid double-counting

    for h, locations in hash_map.items():
        if len(locations) < 2:
            continue
        # Only report if the duplicates are in *different* files
        unique_files = set(loc[0] for loc in locations)
        if len(unique_files) < 2:
            continue

        # Count duplicated lines (every occurrence except the first is "duplication")
        first = locations[0]
        for file_path, label, start, end, content in locations[1:]:
            range_key = (file_path, start, end)
            if range_key not in counted_ranges:
                counted_ranges.add(range_key)
                stats.duplicated_lines += _block_line_count(content, start, end)

            violations.append(
                Violation(
                    rule_id="CROSS001",
                    rule_name="cross_file_duplicate",
                    severity=Severity.WARNING,
                    file_path=file_path,
                    line_number=start,
                    message=(
                        f"Code block '{label}' is a duplicate of '{first[1]}' in {first[0]}:{first[2]}. "
                        "Extract shared logic into a common utility module."
                    ),
                    fix_suggestion="Move the shared logic to a utils/ or helpers/ module and import it in both places.",
                    category="duplication",
                )
            )
    return violations, stats


# ── Missing test file detection ───────────────────────────────────────────────

# Mapping: source patterns → expected test file patterns
_PY_TEST_PATTERNS = [
    (re.compile(r"^(?!test_)(?!.*_test\.py$)(.+)\.py$"), ["test_{stem}.py", "{stem}_test.py"]),
]
_JS_TEST_PATTERNS = [
    (re.compile(r"^(?!.*\.(test|spec)\.)(.+)\.(js|jsx|ts|tsx)$"), ["{stem}.test.{ext}", "{stem}.spec.{ext}"]),
]


def detect_missing_test_files(
    files: List[str],
    project_root: str,
    language: str,
) -> List[Violation]:
    """Flag source files that have no corresponding test file anywhere in the project."""
    violations: List[Violation] = []

    # Build a set of all filenames in the project for fast lookup
    all_files_set: Set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(project_root):
        # Skip common non-source dirs
        rel = os.path.relpath(dirpath, project_root)
        if any(seg in rel.split(os.sep) for seg in ("node_modules", "venv", ".git", "__pycache__", "dist", "build")):
            continue
        for fn in filenames:
            all_files_set.add(fn.lower())

    # Only check files that are part of the current review
    exclude_dirs = {"test", "tests", "__tests__", "spec", "migrations", "node_modules", "venv"}

    for file_path in files:
        path = Path(file_path)
        # Skip files already in test directories
        if any(part.lower() in exclude_dirs for part in path.parts):
            continue
        # Skip __init__.py, config, etc.
        if path.name in ("__init__.py", "conftest.py", "setup.py", "manage.py"):
            continue

        stem = path.stem
        ext = path.suffix.lstrip(".")
        has_test = False

        if language == "python" and ext == "py":
            candidates = [f"test_{stem}.py", f"{stem}_test.py"]
        elif language in ("javascript", "typescript") and ext in ("js", "jsx", "ts", "tsx"):
            candidates = [
                f"{stem}.test.{ext}", f"{stem}.spec.{ext}",
                f"{stem}.test.js", f"{stem}.spec.js",
                f"{stem}.test.ts", f"{stem}.spec.ts",
            ]
        else:
            continue

        for candidate in candidates:
            if candidate.lower() in all_files_set:
                has_test = True
                break

        if not has_test:
            violations.append(
                Violation(
                    rule_id="CROSS002",
                    rule_name="missing_test_file",
                    severity=Severity.WARNING,
                    file_path=file_path,
                    line_number=0,
                    message=(
                        f"No test file found for '{path.name}'. "
                        f"Expected one of: {', '.join(candidates[:3])}"
                    ),
                    fix_suggestion=f"Create a test file, e.g. tests/{candidates[0]}",
                    category="test_coverage",
                )
            )
    return violations


# ── Architectural suggestions ─────────────────────────────────────────────────

# Expected directories per framework
_EXPECTED_STRUCTURE: Dict[str, Dict[str, List[str]]] = {
    "fastapi": {
        "required_dirs": ["app", "app/core", "app/models"],
        "required_files": ["app/main.py"],
        "suggestion": "FastAPI projects should follow: app/core/, app/features/<feature>/, app/models/, app/main.py",
    },
    "django": {
        "required_dirs": [],
        "required_files": ["manage.py"],
        "suggestion": "Django projects should have manage.py, <app>/models.py, <app>/views.py, <app>/urls.py",
    },
    "express": {
        "required_dirs": ["src"],
        "required_files": [],
        "suggestion": "Express projects should follow: src/config/, src/features/<feature>/, src/middlewares/, src/models/, src/utils/",
    },
    "react": {
        "required_dirs": ["src", "src/components"],
        "required_files": [],
        "suggestion": "React projects should follow: src/components/, src/pages/, src/hooks/, src/services/, src/store/, src/utils/",
    },
    "nextjs": {
        "required_dirs": ["app", "src"],
        "required_files": [],
        "suggestion": "Next.js projects should follow: app/ (page.tsx, layout.tsx), src/components/, src/services/, src/hooks/",
    },
    "react_native": {
        "required_dirs": ["src", "src/components", "src/screens"],
        "required_files": [],
        "suggestion": "React Native: src/components/, src/screens/, src/navigation/, src/services/, src/store/, src/hooks/",
    },
    "flask": {
        "required_dirs": [],
        "required_files": ["app.py"],
        "suggestion": "Flask projects should follow: app/ or a single app.py with blueprints for features.",
    },
}

# Files that every project should have
_UNIVERSAL_REQUIRED_FILES = [
    (".gitignore", "Every project should have a .gitignore to prevent committing unwanted files."),
    ("README.md", "Every project should have a README.md documenting setup and usage."),
]

_PYTHON_REQUIRED_FILES = [
    ("requirements.txt", "Python projects should have requirements.txt (or pyproject.toml) listing dependencies."),
]

_JS_REQUIRED_FILES = [
    ("package.json", "JavaScript/TypeScript projects must have a package.json."),
]


def detect_architecture_issues(
    project_root: str,
    language: str,
    framework: Optional[str],
    files: List[str],
) -> List[Violation]:
    """Check for missing essential files and expected directory structure."""
    violations: List[Violation] = []
    root = Path(project_root)

    # 1. Universal required files
    for filename, msg in _UNIVERSAL_REQUIRED_FILES:
        if not (root / filename).exists():
            violations.append(
                Violation(
                    rule_id="ARCH001",
                    rule_name="missing_essential_file",
                    severity=Severity.WARNING,
                    file_path=filename,
                    line_number=0,
                    message=f"Missing '{filename}': {msg}",
                    fix_suggestion=f"Create {filename} in the project root.",
                    category="architecture",
                )
            )

    # 2. Language-specific required files
    lang_files = _PYTHON_REQUIRED_FILES if language == "python" else _JS_REQUIRED_FILES
    for filename, msg in lang_files:
        if not (root / filename).exists():
            # Check alternatives
            alt_exists = False
            if filename == "requirements.txt":
                alt_exists = (root / "pyproject.toml").exists() or (root / "setup.py").exists()
            if not alt_exists:
                violations.append(
                    Violation(
                        rule_id="ARCH002",
                        rule_name="missing_dependency_file",
                        severity=Severity.WARNING,
                        file_path=filename,
                        line_number=0,
                        message=f"Missing '{filename}': {msg}",
                        fix_suggestion=f"Create {filename} in the project root.",
                        category="architecture",
                    )
                )

    # 3. .env.example should exist if .env is in .gitignore
    gitignore_path = root / ".gitignore"
    if gitignore_path.exists():
        gitignore_content = gitignore_path.read_text(errors="replace")
        if ".env" in gitignore_content and not (root / ".env.example").exists():
            violations.append(
                Violation(
                    rule_id="ARCH003",
                    rule_name="missing_env_example",
                    severity=Severity.WARNING,
                    file_path=".env.example",
                    line_number=0,
                    message="'.env' is in .gitignore but no .env.example file exists for onboarding.",
                    fix_suggestion="Create .env.example with placeholder values so new developers know which env vars to set.",
                    category="architecture",
                )
            )

    # 4. Framework-specific structure
    if framework and framework in _EXPECTED_STRUCTURE:
        spec = _EXPECTED_STRUCTURE[framework]

        missing_dirs = []
        for d in spec.get("required_dirs", []):
            # Check if at least one of the alternatives exists (src or app)
            if not (root / d).is_dir():
                missing_dirs.append(d)

        missing_files = []
        for f in spec.get("required_files", []):
            if not (root / f).exists():
                missing_files.append(f)

        if missing_dirs or missing_files:
            missing_items = missing_dirs + missing_files
            violations.append(
                Violation(
                    rule_id="ARCH004",
                    rule_name="framework_structure_mismatch",
                    severity=Severity.INFO,
                    file_path=project_root,
                    line_number=0,
                    message=(
                        f"Framework '{framework}' structure: missing {', '.join(missing_items)}. "
                        f"{spec.get('suggestion', '')}"
                    ),
                    fix_suggestion=spec.get("suggestion", "Follow framework conventions for project layout."),
                    category="architecture",
                )
            )

    # 5. Large file detection (> 300 lines)
    for file_path in files:
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
            line_count = len(content.splitlines())
            if line_count > 300:
                violations.append(
                    Violation(
                        rule_id="ARCH005",
                        rule_name="large_file",
                        severity=Severity.WARNING,
                        file_path=file_path,
                        line_number=0,
                        message=(
                            f"File has {line_count} lines (threshold: 300). "
                            "Split into smaller modules with single responsibilities."
                        ),
                        fix_suggestion="Extract related functions into separate files following the Single Responsibility Principle.",
                        category="architecture",
                    )
                )
        except OSError:
            continue

    return violations
