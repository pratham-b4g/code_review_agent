"""AI-powered deep code review using Claude or OpenAI API."""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from agent.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_MODEL   = "gpt-4o-mini"   # default to OpenAI (free credits available)
_CLAUDE_MODEL    = "claude-haiku-4-5-20251001"
_MAX_TOKENS      = 4096
_MAX_FILE_CHARS  = 2000   # chars per file — prevents huge token bills
_MAX_TOTAL_CHARS = 20000  # hard cap across all files

_RED    = "\033[91m"
_YELLOW = "\033[93m"
_GREEN  = "\033[92m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

_AI_CHECKS_FILE = Path(__file__).parent / "ai_checks.yaml"


def _load_checks() -> str:
    """Load check instructions from ai_checks.yaml."""
    if not _AI_CHECKS_FILE.exists():
        return "Analyse for security, performance, code quality, and structure issues."
    try:
        import yaml
        with open(_AI_CHECKS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        checks = data.get("checks", [])
        lines = []
        for i, check in enumerate(checks, 1):
            lines.append(f"{i}. **{check['name']}** — {check['description'].strip()}")
        return "\n".join(lines)
    except Exception:
        return "Analyse for security, performance, code quality, and structure issues."


_EXCLUDE_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__", ".git",
    "dist", "build", ".next", "coverage", ".pytest_cache",
    ".idea", ".vscode", "logs", "tmp",
}


# ── Public entry point ────────────────────────────────────────────────────────

def run_ai_review(
    files: List[str],
    project_root: str,
    language: str,
    framework: Optional[str],
    api_key: Optional[str] = None,
    model: str = _DEFAULT_MODEL,
) -> int:
    """Run AI-powered deep review using OpenAI or Claude API (auto-detected by key).

    Returns:
        0 — no high-severity issues found
        1 — high-severity issues found
    """
    # Resolve API key — priority: Groq > Gemini > OpenAI > Claude
    groq_key   = api_key or os.getenv("GROQ_API_KEY")
    gemini_key = None if groq_key else (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    openai_key = None if (groq_key or gemini_key) else os.getenv("OPENAI_API_KEY")
    claude_key = None if (groq_key or gemini_key or openai_key) else os.getenv("ANTHROPIC_API_KEY")

    key = groq_key or gemini_key or openai_key or claude_key
    if not key:
        print(
            f"{_YELLOW}[AI] No API key found. Set GROQ_API_KEY, GEMINI_API_KEY, "
            f"OPENAI_API_KEY, or ANTHROPIC_API_KEY as an environment variable.{_RESET}"
        )
        return 0

    file_contents = _read_files(files, project_root)
    if not file_contents:
        print(f"{_YELLOW}[AI] No readable files found — skipping AI review.{_RESET}")
        return 0

    folder_structure = _get_folder_structure(project_root)
    prompt = _build_prompt(file_contents, folder_structure, language, framework)

    # Detect provider
    if groq_key:
        provider_name = "Groq"
    elif gemini_key:
        provider_name = "Gemini"
    elif openai_key:
        provider_name = "OpenAI"
    else:
        provider_name = "Claude"

    print(f"\n{_CYAN}{_BOLD}── AI Deep Review ({provider_name}) {'─' * 40}{_RESET}")
    print(f"{_CYAN}[AI] Analysing {len(file_contents)} file(s) — please wait...{_RESET}")

    try:
        if groq_key:
            response_text = _call_groq(groq_key, prompt)
        elif gemini_key:
            response_text = _call_gemini(gemini_key, "gemini-2.0-flash", prompt)
        elif openai_key:
            response_text = _call_openai(openai_key, "gpt-4o-mini", prompt)
        else:
            response_text = _call_claude(claude_key, _CLAUDE_MODEL, prompt)
    except Exception as exc:
        print(f"{_YELLOW}[AI] API call failed: {exc}{_RESET}")
        return 0

    return _parse_and_display(response_text)


def _call_groq(key: str, prompt: str) -> str:
    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError("groq not installed. Run: pip install groq")
    client = Groq(api_key=key)
    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def _call_gemini(key: str, model: str, prompt: str) -> str:
    try:
        from google import genai
    except ImportError:
        raise RuntimeError("google-genai not installed. Run: pip install google-genai")
    client = genai.Client(api_key=key)
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text


def _call_openai(key: str, model: str, prompt: str) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    client = OpenAI(api_key=key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def _call_claude(key: str, model: str, prompt: str) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    client = anthropic.Anthropic(api_key=key)
    message = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ── File reading ──────────────────────────────────────────────────────────────

def _read_files(files: List[str], project_root: str) -> Dict[str, str]:
    """Read file contents, truncating large files to stay within token budget."""
    result: Dict[str, str] = {}
    total_chars = 0

    for f in files:
        path = Path(f) if Path(f).is_absolute() else Path(project_root) / f
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + "\n... [truncated]"
            if total_chars + len(content) > _MAX_TOTAL_CHARS:
                break
            total_chars += len(content)
            result[str(f)] = content
        except Exception:
            pass

    return result


# ── Folder structure ──────────────────────────────────────────────────────────

def _get_folder_structure(root: str, max_depth: int = 3) -> str:
    """Generate a compact folder tree string (max 60 lines)."""
    lines: List[str] = [Path(root).name + "/"]

    def _walk(path: Path, depth: int, prefix: str) -> None:
        if depth > max_depth or len(lines) > 60:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return
        visible = [e for e in entries if e.name not in _EXCLUDE_DIRS and not e.name.startswith(".")]
        for i, entry in enumerate(visible):
            is_last = i == len(visible) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}")
            if entry.is_dir():
                extension = "    " if is_last else "│   "
                _walk(entry, depth + 1, prefix + extension)

    _walk(Path(root), 1, "")
    return "\n".join(lines)


# ── Prompt ────────────────────────────────────────────────────────────────────

def _build_prompt(
    file_contents: Dict[str, str],
    folder_structure: str,
    language: str,
    framework: Optional[str],
) -> str:
    files_block = "\n\n".join(
        f"### {path}\n```\n{content}\n```"
        for path, content in file_contents.items()
    )

    checks = _load_checks()

    return f"""You are a senior software engineer doing a thorough production-grade code review.
Project: {language} / {framework or 'no specific framework'}

## Folder Structure
```
{folder_structure}
```

## Changed Files
{files_block}

## Task
Analyse the code deeply across ALL the following dimensions:

{checks}

Return ONLY a valid JSON object — no markdown fences, no text outside the JSON.

{{
  "quality_score": <integer 1-10>,
  "summary": "<2-3 sentence overall assessment>",
  "issues": [
    {{
      "severity": "<high|medium|low>",
      "category": "<security|duplicate_code|function_too_large|file_too_large|folder_structure|performance|error_handling|naming|dead_code|missing_validation>",
      "file": "<filename or 'project'>",
      "line": <line number or null>,
      "problem": "<clear explanation of the issue>",
      "fix": "<concrete actionable fix>"
    }}
  ],
  "large_files": [
    {{
      "file": "<filename>",
      "estimated_lines": <number>,
      "suggestion": "<how to split it>"
    }}
  ],
  "large_functions": [
    {{
      "file": "<filename>",
      "function": "<function name>",
      "problem": "<why it should be split>",
      "suggestion": "<how to split it>"
    }}
  ],
  "duplicate_code": [
    {{
      "description": "<what logic is duplicated>",
      "locations": ["<file:line>", "<file:line>"],
      "fix": "<extract into shared function/module>"
    }}
  ],
  "folder_structure_issues": ["<specific issue with current layout and how to fix it>"],
  "files_to_remove": ["<files that should NOT be in git e.g. .env, secrets.json>"],
  "files_to_add": ["<files missing from repo e.g. .env.example, README.md>"],
  "gitignore_corrections": ["<e.g. Add: .env>"],
  "quick_wins": ["<easy improvement that takes < 10 mins>"],
  "major_risks": ["<things that could cause production incidents>"],
  "refactoring_roadmap": [
    "Step 1: <highest priority>",
    "Step 2: <next priority>"
  ]
}}"""


# ── Output rendering ──────────────────────────────────────────────────────────

def _parse_and_display(response_text: str) -> int:
    """Parse Claude's JSON response and print formatted terminal output."""
    text = response_text.strip()
    start = text.find("{")
    end   = text.rfind("}") + 1

    if start == -1 or end == 0:
        print(f"{_YELLOW}[AI] Could not parse response:\n{text[:500]}{_RESET}")
        return 0

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        print(f"{_YELLOW}[AI] JSON parse error: {exc}{_RESET}")
        return 0

    has_high = False

    # ── Quality score ──
    score = data.get("quality_score", "?")
    if isinstance(score, int):
        color = _GREEN if score >= 7 else (_YELLOW if score >= 5 else _RED)
    else:
        color = _RESET
    print(f"\n  {_BOLD}Quality Score: {color}{score} / 10{_RESET}")

    # ── Summary ──
    summary = data.get("summary", "")
    if summary:
        print(f"\n  {summary}")

    # ── Issues by severity ──
    issues = data.get("issues", [])
    high   = [i for i in issues if i.get("severity") == "high"]
    medium = [i for i in issues if i.get("severity") == "medium"]
    low    = [i for i in issues if i.get("severity") == "low"]

    if high:
        has_high = True
        print(f"\n  {_RED}{_BOLD}● HIGH  (Critical / Security / Breaking){_RESET}")
        for issue in high:
            _print_issue(issue, _RED)

    if medium:
        print(f"\n  {_YELLOW}{_BOLD}● MEDIUM  (Performance / Maintainability){_RESET}")
        for issue in medium:
            _print_issue(issue, _YELLOW)

    if low:
        print(f"\n  {_CYAN}{_BOLD}● LOW  (Style / Minor Improvements){_RESET}")
        for issue in low:
            _print_issue(issue, _CYAN)

    if not issues:
        print(f"\n  {_GREEN}No issues detected.{_RESET}")

    # ── Large files ──
    large_files = data.get("large_files", [])
    if large_files:
        print(f"\n  {_YELLOW}{_BOLD}● Large Files (consider splitting):{_RESET}")
        for lf in large_files:
            print(f"\n    📄 {lf.get('file')}  (~{lf.get('estimated_lines')} lines)")
            print(f"    Suggestion: {lf.get('suggestion')}")

    # ── Large functions ──
    large_fns = data.get("large_functions", [])
    if large_fns:
        print(f"\n  {_YELLOW}{_BOLD}● Large Functions (consider splitting):{_RESET}")
        for fn in large_fns:
            print(f"\n    ƒ  {fn.get('function')}()  in {fn.get('file')}")
            print(f"    Problem: {fn.get('problem')}")
            print(f"    Fix:     {fn.get('suggestion')}")

    # ── Duplicate code ──
    dupes = data.get("duplicate_code", [])
    if dupes:
        print(f"\n  {_YELLOW}{_BOLD}● Duplicate Code / Redundancy:{_RESET}")
        for d in dupes:
            locs = ", ".join(d.get("locations", []))
            print(f"\n    ⟳  {d.get('description')}")
            print(f"    Locations: {locs}")
            print(f"    Fix:       {d.get('fix')}")

    # ── Folder structure ──
    folder_issues = data.get("folder_structure_issues", [])
    if folder_issues:
        print(f"\n  {_YELLOW}{_BOLD}● Folder Structure Issues:{_RESET}")
        for fi in folder_issues:
            print(f"    →  {fi}")

    # ── Git / repo hygiene ──
    to_remove = data.get("files_to_remove", [])
    if to_remove:
        print(f"\n  {_RED}{_BOLD}Files to REMOVE from git:{_RESET}")
        for f in to_remove:
            print(f"    ✖  {f}")

    to_add = data.get("files_to_add", [])
    if to_add:
        print(f"\n  {_YELLOW}{_BOLD}Files MISSING from repo:{_RESET}")
        for f in to_add:
            print(f"    +  {f}")

    gitignore = data.get("gitignore_corrections", [])
    if gitignore:
        print(f"\n  {_YELLOW}{_BOLD}.gitignore corrections:{_RESET}")
        for line in gitignore:
            print(f"    →  {line}")

    # ── Highlights ──
    wins = data.get("quick_wins", [])
    if wins:
        print(f"\n  {_GREEN}{_BOLD}Quick wins:{_RESET}")
        for w in wins:
            print(f"    ✓  {w}")

    risks = data.get("major_risks", [])
    if risks:
        print(f"\n  {_RED}{_BOLD}Major production risks:{_RESET}")
        for r in risks:
            print(f"    ⚠  {r}")

    # ── Roadmap ──
    roadmap = data.get("refactoring_roadmap", [])
    if roadmap:
        print(f"\n  {_CYAN}{_BOLD}Refactoring roadmap:{_RESET}")
        for step in roadmap:
            print(f"    {step}")

    # ── Footer ──
    total = len(issues)
    print(f"\n  {'─' * 60}")
    print(f"  {len(high)} high  |  {len(medium)} medium  |  {len(low)} low  |  {total} total issue(s)")

    if has_high:
        print(f"  {_RED}{_BOLD}🚫  High severity issues found — fix before merging.{_RESET}\n")
    else:
        print(f"  {_GREEN}✅  No high severity issues — good to go.{_RESET}\n")

    return 1 if has_high else 0


def _print_issue(issue: dict, color: str) -> None:
    file_ref = issue.get("file", "")
    line     = issue.get("line")
    location = f"{file_ref}:{line}" if line else file_ref
    category = issue.get("category", "")
    print(f"\n    📄 {location}  [{category}]")
    print(f"    {color}Problem:{_RESET} {issue.get('problem', '')}")
    print(f"    Fix:     {issue.get('fix', '')}")
