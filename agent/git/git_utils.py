"""Git utilities for retrieving files involved in a push or staged for commit."""

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from agent.utils.logger import get_logger

logger = get_logger(__name__)

_ZERO_SHA = "0" * 40


def _run_git(*args: str, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        logger.error("git executable not found in PATH")
        return 1, "", "git not found"


def get_repo_root() -> Optional[str]:
    """Return the absolute path of the git repository root."""
    code, out, _ = _run_git("rev-parse", "--show-toplevel")
    return out if code == 0 else None


def get_staged_files(cwd: Optional[str] = None) -> List[str]:
    """Return a list of staged (index) files as absolute paths."""
    code, out, err = _run_git("diff", "--cached", "--name-only", "--diff-filter=ACMRT", cwd=cwd)
    if code != 0:
        logger.warning("Failed to list staged files: %s", err)
        return []
    files = [f for f in out.splitlines() if f]
    if cwd and files:
        # Resolve relative paths to absolute using the repo root
        root_code, root_out, _ = _run_git("rev-parse", "--show-toplevel", cwd=cwd)
        repo_root = root_out if root_code == 0 else cwd
        files = [str(Path(repo_root) / f) for f in files]
    return files


def get_pushed_files(local_sha: str, remote_sha: str) -> List[str]:
    """Return files changed between remote_sha and local_sha.

    Handles new branches where remote_sha is all zeros.

    Args:
        local_sha: SHA of the local ref being pushed.
        remote_sha: SHA of the remote ref (40 zeros for new branches).

    Returns:
        List of changed file paths.
    """
    if remote_sha == _ZERO_SHA:
        # New branch — compare against the nearest merge-base with main/master
        for base in ("origin/main", "origin/master", "main", "master"):
            code, merge_base, _ = _run_git("merge-base", local_sha, base)
            if code == 0 and merge_base:
                remote_sha = merge_base
                logger.debug("New branch; using merge-base %s with %s", merge_base, base)
                break
        else:
            # Fallback: list all files tracked by git
            code, out, _ = _run_git("ls-files")
            return [f for f in out.splitlines() if f] if code == 0 else []

    code, out, err = _run_git(
        "diff", "--name-only", "--diff-filter=ACMRT", f"{remote_sha}..{local_sha}"
    )
    if code != 0:
        logger.warning("Failed to diff %s..%s: %s", remote_sha, local_sha, err)
        return []
    return [f for f in out.splitlines() if f]


def parse_pre_push_stdin() -> List[Tuple[str, str, str, str]]:
    """Parse stdin provided by git pre-push hook.

    Returns:
        List of (local_ref, local_sha, remote_ref, remote_sha) tuples.
    """
    refs: List[Tuple[str, str, str, str]] = []
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) == 4:
            refs.append((parts[0], parts[1], parts[2], parts[3]))
    return refs


def _filter_gitignored(files: List[str], cwd: Optional[str] = None) -> List[str]:
    """Remove files that .gitignore marks as ignored.

    Uses ``git check-ignore`` so the result is always consistent with git's
    own view of the repository.  If the command is unavailable the original
    list is returned unchanged so the caller is never blocked.
    """
    if not files:
        return files
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(files),
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        ignored = set(result.stdout.split("\0")) if result.stdout else set()
        return [f for f in files if f not in ignored]
    except Exception:
        return files  # never block a review because check-ignore is unavailable


def collect_files_for_push() -> List[str]:
    """Entry point for the pre-push hook to collect all files being pushed.

    Returns:
        Deduplicated list of file paths that will be pushed.
    """
    refs = parse_pre_push_stdin()
    if not refs:
        logger.debug("No refs received from git; falling back to staged files")
        return get_staged_files()

    all_files: List[str] = []
    for local_ref, local_sha, remote_ref, remote_sha in refs:
        if local_sha == _ZERO_SHA:
            # Deleting a remote branch — nothing to review
            continue
        logger.debug("Collecting files for %s (%s → %s)", local_ref, remote_sha[:8], local_sha[:8])
        all_files.extend(get_pushed_files(local_sha, remote_sha))

    # Deduplicate while preserving order
    seen = set()
    unique: List[str] = []
    for f in all_files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return _filter_gitignored(unique)


def get_changed_lines(file_path: str, cwd: Optional[str] = None) -> Optional[set]:
    """Return a set of 1-indexed line numbers that were changed in the working tree.

    Uses ``git diff -U0`` to find only added/modified lines.  Returns None if
    the diff cannot be computed (e.g. new untracked file) — the caller should
    treat None as "check all lines".
    """
    # Try staged diff first, then HEAD diff
    for diff_args in (
        ["diff", "--cached", "-U0", "--", file_path],
        ["diff", "HEAD", "-U0", "--", file_path],
        ["diff", "-U0", "--", file_path],
    ):
        code, out, _ = _run_git(*diff_args, cwd=cwd)
        if code == 0 and out.strip():
            break
    else:
        return None  # file might be new/untracked — check everything

    changed: set = set()
    for line in out.splitlines():
        # Unified diff hunk header: @@ -old_start,old_count +new_start,new_count @@
        if not line.startswith("@@"):
            continue
        # Extract the +side (new file lines)
        import re as _re
        m = _re.search(r"\+(\d+)(?:,(\d+))?", line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            for i in range(start, start + count):
                changed.add(i)
    return changed if changed else None


def get_changed_lines_between(
    file_path: str,
    base_ref: str = "HEAD",
    cwd: Optional[str] = None,
) -> Optional[set]:
    """Return changed lines between a base ref and the working tree for a single file."""
    code, out, _ = _run_git("diff", base_ref, "-U0", "--", file_path, cwd=cwd)
    if code != 0 or not out.strip():
        return None
    changed: set = set()
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        import re as _re
        m = _re.search(r"\+(\d+)(?:,(\d+))?", line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            for i in range(start, start + count):
                changed.add(i)
    return changed if changed else None


def file_exists_in_repo(rel_path: str) -> bool:
    """Return True if a file exists at the given path relative to CWD."""
    return Path(rel_path).is_file()


_SOURCE_EXTENSIONS = {
    "python":     {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
}

_DEFAULT_EXCLUDE_DIRS = {
    "node_modules", "dist", "build", ".next", ".nuxt", ".vite",
    "coverage", ".git", "__pycache__", ".mypy_cache", "venv", ".venv",
    "vendor", "out", ".turbo", "storybook-static",
    ".gemini", ".claude", ".idea", ".vscode", ".cache",
    "tmp", "temp", ".tmp", "logs",
}


def scan_directory(
    root: str,
    language: str = "python",
    extra_excludes: Optional[List[str]] = None,
) -> List[str]:
    """Recursively scan *root* and return all source files for *language*.

    Args:
        root: Directory to scan.
        language: Determines which file extensions to collect.
        extra_excludes: Additional directory names to skip.

    Returns:
        Sorted list of absolute file paths.
    """
    exts = _SOURCE_EXTENSIONS.get(language, _SOURCE_EXTENSIONS["python"])
    excluded = _DEFAULT_EXCLUDE_DIRS | set(extra_excludes or [])
    results: List[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs in-place so os.walk won't descend into them
        dirnames[:] = [d for d in dirnames if d not in excluded]
        for fname in filenames:
            if Path(fname).suffix.lower() in exts:
                results.append(str(Path(dirpath) / fname))

    return sorted(_filter_gitignored(results, root))
