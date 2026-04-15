"""Runs ESLint (JS/TS) or ruff/flake8 (Python) on the files being committed."""

import re
import subprocess
import shutil
import sys
import os
from pathlib import Path
from typing import List, Optional

from agent.utils.logger import get_logger

logger = get_logger(__name__)

# ANSI colours
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"


def run_linting(
    files: List[str],
    language: str,
    project_root: str,
    framework: Optional[str] = None,
    python_linter: str = "auto",   # "flake8" | "ruff" | "auto"
    js_linter: str = "eslint",
) -> int:
    """Run the appropriate linter for the given language.

    Args:
        files:         List of file paths to lint.
        language:      Detected project language.
        project_root:  Root directory of the project.
        framework:     Detected framework (used to generate ESLint config).
        python_linter: Which Python linter to use ("auto" tries ruff then flake8).
        js_linter:     Which JS linter to use (currently only "eslint").

    Returns:
        0 if linting passed, 1 if there are errors.
    """
    py_files = [f for f in files if f.endswith(".py")]
    js_files = [f for f in files if Path(f).suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs"}]

    exit_code = 0

    if py_files:
        exit_code |= _run_python_linter(py_files, python_linter)

    if js_files:
        exit_code |= _run_eslint(js_files, project_root, js_linter, framework)

    return exit_code


# ── Python ────────────────────────────────────────────────────────────────────

def _run_python_linter(files: List[str], linter: str) -> int:
    """Run ruff or flake8 on Python files.

    ruff is a required dependency so it is always available — no skip path.
    """
    tool = _pick_python_linter(linter)

    if tool is None:
        # Only reachable when preference="flake8" and flake8 is not installed
        print(
            f"{_YELLOW}[LINT] flake8 not found. "
            f"Falling back to ruff (bundled).{_RESET}"
        )
        tool = f"{sys.executable} -m ruff"

    label = "ruff" if "ruff" in tool else "flake8"
    print(f"\n{_CYAN}{_BOLD}── Python Linting ({label}) {'─' * 40}{_RESET}")

    if "flake8" in tool:
        return _run_subprocess([tool, "--max-line-length=120"] + files)
    else:
        # ruff — works both as "ruff" binary and "python -m ruff"
        cmd = tool.split() + ["check", "--output-format=concise"] + files
        return _run_subprocess(cmd)


def _pick_python_linter(preference: str) -> Optional[str]:
    """Return the linter binary to use.

    ruff is a required dependency so it is always available via
    'python -m ruff' even if the ruff binary is not on PATH.
    """
    if preference == "flake8":
        return "flake8" if shutil.which("flake8") else None

    # ruff: try the binary first, fall back to python -m ruff
    if shutil.which("ruff"):
        return "ruff"
    # ruff is bundled as a dependency — always runnable via the current interpreter
    return f"{sys.executable} -m ruff"


# ── JavaScript / TypeScript ───────────────────────────────────────────────────

def _run_eslint(
    files: List[str],
    project_root: str,
    linter: str,
    framework: Optional[str] = None,
) -> int:
    """Run ESLint on JS/TS files.

    If ESLint is not installed but npm is available, installs it automatically
    and creates a framework-appropriate .eslintrc.json config.
    Groups files by their nearest ESLint config root for monorepo support.
    """
    # Auto-setup ESLint if missing (requires npm)
    _ensure_eslint(project_root, framework)

    # Resolve all paths to absolute so ESLint receives unambiguous paths
    abs_files = [
        str(Path(f) if Path(f).is_absolute() else Path(project_root) / f)
        for f in files
    ]

    # Group files by the config root nearest to them
    groups: dict = {}
    for f in abs_files:
        config_root = _find_eslint_config_root(f, project_root)
        if config_root:
            groups.setdefault(config_root, []).append(f)

    if not groups:
        print(
            f"{_YELLOW}[LINT] No ESLint config found in {project_root} "
            f"or any subdirectory. Skipping JS linting.{_RESET}"
        )
        return 0

    exit_code = 0
    print(f"\n{_CYAN}{_BOLD}── JavaScript Linting (ESLint) {'─' * 37}{_RESET}")

    for config_root, group_files in groups.items():
        eslint_bin = _find_eslint(config_root)
        if eslint_bin is None:
            print(
                f"{_YELLOW}[LINT] ESLint not found in {config_root}. "
                f"Install it: npm install --save-dev eslint{_RESET}"
            )
            continue
        exit_code |= _run_subprocess(
            [eslint_bin, "--format=stylish"] + group_files,
            cwd=config_root,
        )

    return exit_code


def _ensure_eslint(project_root: str, framework: Optional[str]) -> None:
    """Auto-install ESLint and create a default config if missing.

    Only runs when:
      - No ESLint binary is found in the project
      - npm is available on PATH
      - A package.json exists in the project root
    """
    if _find_eslint(project_root) is not None:
        # Ensure unused-imports plugin is installed for --fix to remove unused imports
        _ensure_unused_imports_plugin(project_root)
        return  # Already installed

    if not shutil.which("npm"):
        return  # npm not available — can't auto-install

    pkg_json = Path(project_root) / "package.json"
    if not pkg_json.exists():
        return  # Not an npm project

    print(f"{_CYAN}[LINT] ESLint not found — installing automatically...{_RESET}")
    result = subprocess.run(
        ["npm", "install", "--save-dev", "eslint", "eslint-plugin-unused-imports"],
        cwd=project_root,
        capture_output=True,
        text=True,
        shell=True,
    )
    if result.returncode != 0:
        print(f"{_YELLOW}[LINT] ESLint auto-install failed: {result.stderr.strip()}{_RESET}")
        return

    print(f"{_CYAN}[LINT] ESLint installed successfully.{_RESET}")

    # Create default config if none exists
    if not _has_eslint_config(project_root):
        _create_eslint_config(project_root, framework)


def _ensure_unused_imports_plugin(project_root: str) -> None:
    """Install eslint-plugin-unused-imports if not already present.

    This plugin is needed for ESLint --fix to auto-remove unused imports
    (e.g. importing useEffect in React/Next.js but not using it).
    """
    plugin_path = Path(project_root) / "node_modules" / "eslint-plugin-unused-imports"
    if plugin_path.exists():
        # Plugin installed, but ensure ESLint config exists and has the rules
        if not _has_eslint_config(project_root):
            _create_eslint_config(project_root, framework=None)
            print(f"{_CYAN}[LINT] Created .eslintrc.json with unused-imports rules.{_RESET}")
        return  # Already installed

    pkg_json = Path(project_root) / "package.json"
    if not pkg_json.exists():
        return

    if not shutil.which("npm"):
        return

    print(f"{_CYAN}[LINT] Installing eslint-plugin-unused-imports for auto-fix support...{_RESET}")
    result = subprocess.run(
        ["npm", "install", "--save-dev", "eslint-plugin-unused-imports"],
        cwd=project_root,
        capture_output=True,
        text=True,
        shell=True,
    )
    if result.returncode != 0:
        print(f"{_YELLOW}[LINT] Plugin install failed: {result.stderr.strip()}{_RESET}")
        return
    print(f"{_CYAN}[LINT] eslint-plugin-unused-imports installed.{_RESET}")

    # Patch existing eslint config or create one if none exists
    if _has_eslint_config(project_root):
        _patch_eslint_config_with_unused_imports(project_root)
    else:
        _create_eslint_config(project_root, framework=None)
        print(f"{_CYAN}[LINT] Created .eslintrc.json with unused-imports rules (no config existed).{_RESET}")


def _patch_eslint_config_with_unused_imports(project_root: str) -> None:
    """Add unused-imports plugin rules to an existing ESLint config.

    Supports both legacy .eslintrc.json and the new flat config (eslint.config.mjs).
    """
    import json

    # Try legacy .eslintrc.json first
    config_path = Path(project_root) / ".eslintrc.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return

        plugins = config.get("plugins", [])
        if "unused-imports" not in plugins:
            plugins.append("unused-imports")
            config["plugins"] = plugins

        rules = config.get("rules", {})
        if "unused-imports/no-unused-imports" not in rules:
            rules["no-unused-vars"] = "off"
            rules["unused-imports/no-unused-imports"] = "error"
            rules["unused-imports/no-unused-vars"] = [
                "warn",
                {"vars": "all", "varsIgnorePattern": "^_", "args": "after-used", "argsIgnorePattern": "^_"},
            ]
            config["rules"] = rules

        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print(f"{_CYAN}[LINT] Patched .eslintrc.json with unused-imports plugin rules.{_RESET}")
        return

    # Try flat config (eslint.config.mjs / eslint.config.js)
    for flat_name in ("eslint.config.mjs", "eslint.config.js"):
        flat_path = Path(project_root) / flat_name
        if flat_path.exists():
            content = flat_path.read_text(encoding="utf-8")
            if "unused-imports" in content:
                return  # Already patched

            # Inject the plugin import and rule block
            patch_import = 'import unusedImports from "eslint-plugin-unused-imports";\n'
            patch_block = """
{
  plugins: {
    "unused-imports": unusedImports,
  },
  rules: {
    "no-unused-vars": "off",
    "unused-imports/no-unused-imports": "error",
    "unused-imports/no-unused-vars": ["warn", { vars: "all", varsIgnorePattern: "^_", args: "after-used", argsIgnorePattern: "^_" }],
  },
},
"""
            # Insert import at the top (after existing imports)
            if "import " in content:
                # Add after last import
                lines = content.splitlines(keepends=True)
                last_import_idx = 0
                for idx, line in enumerate(lines):
                    if line.strip().startswith("import "):
                        last_import_idx = idx
                lines.insert(last_import_idx + 1, patch_import)
                content = "".join(lines)
            else:
                content = patch_import + content

            # Insert rule block before the closing ];
            if "];" in content:
                content = content.replace("];", patch_block + "];", 1)

            flat_path.write_text(content, encoding="utf-8")
            print(f"{_CYAN}[LINT] Patched {flat_name} with unused-imports plugin rules.{_RESET}")
            return


def _create_eslint_config(project_root: str, framework: Optional[str]) -> None:
    """Write a framework-appropriate .eslintrc.json into project_root."""
    import json

    fw = (framework or "").lower()

    # Base config — always included
    config: dict = {
        "extends": ["eslint:recommended"],
        "plugins": ["unused-imports"],
        "parserOptions": {
            "ecmaVersion": "latest",
            "sourceType": "module",
        },
        "rules": {
            "no-unused-vars": "off",
            "unused-imports/no-unused-imports": "error",
            "unused-imports/no-unused-vars": [
                "warn",
                {"vars": "all", "varsIgnorePattern": "^_", "args": "after-used", "argsIgnorePattern": "^_"},
            ],
        },
    }

    if fw in ("react", "react_native"):
        config["env"] = {"browser": True, "es2021": True}
        config["parserOptions"]["ecmaFeatures"] = {"jsx": True}

    elif fw in ("nextjs", "next"):
        config["env"] = {"browser": True, "node": True, "es2021": True}

    elif fw in ("express", "nodejs", "node"):
        config["env"] = {"node": True, "es2021": True}

    else:
        # Generic JS project
        config["env"] = {"browser": True, "node": True, "es2021": True}

    config_path = Path(project_root) / ".eslintrc.json"
    config_path.write_text(
        json.dumps(config, indent=2),
        encoding="utf-8",
    )
    print(
        f"{_CYAN}[LINT] Created default .eslintrc.json for "
        f"framework='{framework or 'generic'}' in {project_root}{_RESET}"
    )


def _find_eslint_config_root(file_path: str, stop_at: str) -> Optional[str]:
    """Walk upward from file_path until an ESLint config is found or stop_at is reached."""
    current = Path(file_path).resolve().parent
    stop = Path(stop_at).resolve()

    while True:
        if _has_eslint_config(str(current)):
            return str(current)
        if current == stop or current.parent == current:
            break
        current = current.parent

    # Also check stop_at itself
    if _has_eslint_config(str(stop)):
        return str(stop)
    return None


def _find_eslint(project_root: str) -> Optional[str]:
    """Find ESLint binary: local node_modules first, then global."""
    local_win  = Path(project_root) / "node_modules" / ".bin" / "eslint.cmd"
    local_unix = Path(project_root) / "node_modules" / ".bin" / "eslint"
    if local_win.exists():
        return str(local_win)
    if local_unix.exists():
        return str(local_unix)
    if shutil.which("eslint"):
        return "eslint"
    if shutil.which("npx"):
        return "npx eslint"
    return None


def _has_eslint_config(project_root: str) -> bool:
    """Return True if any ESLint config file exists in project_root."""
    config_files = [
        ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
        ".eslintrc.yaml", ".eslintrc.yml", "eslint.config.js", "eslint.config.mjs",
    ]
    root = Path(project_root)
    # Also check if eslintConfig is in package.json
    pkg = root / "package.json"
    if pkg.exists():
        try:
            import json
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if "eslintConfig" in data:
                return True
        except Exception:
            pass
    return any((root / f).exists() for f in config_files)


# ── Autofix ───────────────────────────────────────────────────────────────────

def run_autofix(
    files: List[str],
    language: str,
    project_root: str,
    framework: Optional[str] = None,
    python_linter: str = "auto",
    unsafe_fixes: bool = False,
) -> int:
    """Run auto-fix on the given files: ruff --fix for Python, eslint --fix for JS.

    Args:
        files:          List of file paths to fix.
        language:       Detected project language.
        project_root:   Root directory of the project.
        framework:      Detected framework.
        python_linter:  Which Python linter to use.
        unsafe_fixes:   If True, also apply ruff's unsafe fixes.

    Returns:
        0 if autofix ran successfully, 1 if there were remaining unfixable errors.
    """
    py_files = [f for f in files if f.endswith(".py")]
    js_files = [f for f in files if Path(f).suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs"}]

    fixed_count = 0
    remaining = 0

    if py_files:
        fc, rm = _autofix_python(py_files, python_linter, unsafe_fixes)
        fixed_count += fc
        remaining += rm

    if js_files:
        fc, rm = _autofix_js(js_files, project_root, framework)
        fixed_count += fc
        remaining += rm

    print(f"\n{_CYAN}{_BOLD}── Autofix Summary {'─' * 49}{_RESET}")
    if fixed_count:
        print(f"  {_CYAN}✔ Fixed {fixed_count} issue(s) automatically.{_RESET}")
    if remaining:
        print(f"  {_YELLOW}⚠ {remaining} issue(s) remain and need manual attention.{_RESET}")
    if not fixed_count and not remaining:
        print(f"  {_CYAN}✔ No issues to fix — code is clean.{_RESET}")
    print()

    return 1 if remaining else 0


def _autofix_python(files: List[str], linter: str, unsafe_fixes: bool) -> tuple:
    """Run ruff check --fix and ruff format on Python files.

    Returns:
        (fixed_count, remaining_count)
    """
    tool = _pick_python_linter(linter)
    if tool is None or "flake8" in (tool or ""):
        # flake8 doesn't have --fix; fall back to ruff
        tool = f"{sys.executable} -m ruff"

    label = "ruff"
    print(f"\n{_CYAN}{_BOLD}── Python Autofix ({label}) {'─' * 40}{_RESET}")

    # Step 1: ruff check --fix (lint rules: F401, I001, UP007, UP035, etc.)
    fix_cmd = tool.split() + ["check", "--fix"]
    if unsafe_fixes:
        fix_cmd.append("--unsafe-fixes")
    fix_cmd += files

    print(f"  Running: {label} check --fix{'--unsafe-fixes' if unsafe_fixes else ''}...")
    fix_result = subprocess.run(
        fix_cmd, capture_output=True, text=True,
    )

    # Count fixed issues from ruff output
    fixed = 0
    remaining = 0
    for line in (fix_result.stdout + fix_result.stderr).splitlines():
        if "Fixed" in line:
            # e.g. "Fixed 42 errors."
            m = re.search(r"Fixed (\d+)", line)
            if m:
                fixed += int(m.group(1))
        if "fixable" in line.lower():
            # Still remaining
            pass

    if fix_result.stdout.strip():
        print(fix_result.stdout.strip())
    if fix_result.stderr.strip():
        # ruff prints summary to stderr
        for line in fix_result.stderr.strip().splitlines():
            if line.strip():
                print(f"  {line.strip()}")

    # Step 2: ruff format (whitespace, trailing whitespace, blank lines, etc.)
    fmt_cmd = tool.split() + ["format"] + files
    print(f"  Running: {label} format...")
    fmt_result = subprocess.run(
        fmt_cmd, capture_output=True, text=True,
    )
    formatted = 0
    for line in (fmt_result.stdout + fmt_result.stderr).splitlines():
        if "file" in line.lower() and ("reformatted" in line.lower() or "changed" in line.lower()):
            m = re.search(r"(\d+) file", line)
            if m:
                formatted += int(m.group(1))
    if formatted:
        fixed += formatted
        print(f"  {_CYAN}Formatted {formatted} file(s).{_RESET}")

    # Step 3: re-check to count remaining unfixable issues
    recheck_cmd = tool.split() + ["check", "--output-format=concise"] + files
    recheck = subprocess.run(recheck_cmd, capture_output=True, text=True)
    if recheck.returncode != 0:
        remaining_lines = [l for l in recheck.stdout.strip().splitlines() if l.strip() and ":" in l]
        remaining = len(remaining_lines)
        if remaining:
            print(f"\n  {_YELLOW}Remaining issues ({remaining}):{_RESET}")
            for line in remaining_lines[:20]:
                print(f"    {line}")
            if remaining > 20:
                print(f"    ... and {remaining - 20} more")

    return (fixed, remaining)


def _autofix_js(files: List[str], project_root: str, framework: Optional[str]) -> tuple:
    """Run eslint --fix on JS/TS files.

    Returns:
        (fixed_count, remaining_count)
    """
    _ensure_eslint(project_root, framework)

    abs_files = [
        str(Path(f) if Path(f).is_absolute() else Path(project_root) / f)
        for f in files
    ]

    groups: dict = {}
    for f in abs_files:
        config_root = _find_eslint_config_root(f, project_root)
        if config_root:
            groups.setdefault(config_root, []).append(f)

    if not groups:
        return (0, 0)

    print(f"\n{_CYAN}{_BOLD}── JavaScript Autofix (ESLint) {'─' * 37}{_RESET}")

    total_fixed = 0
    total_remaining = 0

    for config_root, group_files in groups.items():
        eslint_bin = _find_eslint(config_root)
        if eslint_bin is None:
            continue

        cmd = eslint_bin.split() if " " in eslint_bin else [eslint_bin]
        cmd += ["--fix", "--format=stylish"] + group_files

        print(f"  Running: eslint --fix on {len(group_files)} file(s)...")
        result = subprocess.run(cmd, cwd=config_root, capture_output=True, text=True)

        if result.stdout.strip():
            print(result.stdout.strip())

        # Re-check for remaining
        recheck_cmd = (eslint_bin.split() if " " in eslint_bin else [eslint_bin]) + ["--format=compact"] + group_files
        recheck = subprocess.run(recheck_cmd, cwd=config_root, capture_output=True, text=True)
        if recheck.returncode != 0:
            remaining_lines = [l for l in recheck.stdout.strip().splitlines() if l.strip() and ":" in l]
            total_remaining += len(remaining_lines)

    return (total_fixed, total_remaining)


# ── Shared ────────────────────────────────────────────────────────────────────

def _run_subprocess(cmd: List[str], cwd: Optional[str] = None) -> int:
    """Run a command, stream its output, and return the exit code."""
    # Handle "npx eslint" which comes as a single string
    if len(cmd) == 1 and " " in cmd[0]:
        cmd = cmd[0].split() + cmd[1:]

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=False,   # stream output directly to terminal
            text=True,
        )
        if result.returncode != 0:
            print(f"\n{_RED}[LINT] Linting failed — fix the above errors before committing.{_RESET}\n")
        else:
            print(f"{_CYAN}[LINT] No linting errors found.{_RESET}")
        return result.returncode
    except FileNotFoundError:
        logger.warning("Linter binary not found: %s", cmd[0])
        return 0  # Don't block if binary missing
