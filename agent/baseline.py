"""Baseline management — persist known violations per branch so only *new* issues are flagged.

Usage:
    1. Run ``cra baseline save`` to snapshot current violations.
    2. On subsequent runs, load the baseline and subtract known violations.
    3. Only net-new violations are reported / block the commit.

Storage: ``.cra-baseline/<branch>.json`` inside the project root.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from agent.utils.logger import get_logger
from agent.utils.reporter import Severity, Violation

logger = get_logger(__name__)

_BASELINE_DIR = ".cra-baseline"


def _get_current_branch(cwd: Optional[str] = None) -> str:
    """Return the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().replace("/", "_")
    except FileNotFoundError:
        pass
    return "unknown"


def _violation_key(v: Violation) -> str:
    """Deterministic key for a violation (file + rule + message hash)."""
    return f"{v.file_path}::{v.rule_id}::{v.line_number}::{v.message[:80]}"


def _baseline_path(project_root: str, branch: Optional[str] = None) -> Path:
    branch = branch or _get_current_branch(project_root)
    return Path(project_root) / _BASELINE_DIR / f"{branch}.json"


# ── Save baseline ─────────────────────────────────────────────────────────────

def save_baseline(
    project_root: str,
    violations: List[Violation],
    branch: Optional[str] = None,
) -> str:
    """Persist current violations as the baseline for this branch.

    Returns:
        Path to the saved baseline file.
    """
    bp = _baseline_path(project_root, branch)
    bp.parent.mkdir(parents=True, exist_ok=True)

    entries = []
    for v in violations:
        entries.append({
            "key": _violation_key(v),
            "rule_id": v.rule_id,
            "file_path": v.file_path,
            "line_number": v.line_number,
            "message": v.message[:200],
        })

    bp.write_text(json.dumps({"violations": entries}, indent=2), encoding="utf-8")
    logger.info("Saved baseline with %d violations to %s", len(entries), bp)
    return str(bp)


# ── Load baseline ─────────────────────────────────────────────────────────────

def load_baseline(
    project_root: str,
    branch: Optional[str] = None,
) -> Set[str]:
    """Load the violation keys from the baseline file for this branch.

    Returns:
        Set of violation key strings. Empty set if no baseline exists.
    """
    bp = _baseline_path(project_root, branch)
    if not bp.exists():
        return set()

    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
        return {e["key"] for e in data.get("violations", [])}
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        logger.warning("Could not load baseline %s: %s", bp, exc)
        return set()


# ── Filter violations ─────────────────────────────────────────────────────────

def filter_new_violations(
    violations: List[Violation],
    baseline_keys: Set[str],
) -> Tuple[List[Violation], int]:
    """Remove violations that already exist in the baseline.

    Returns:
        (new_violations, suppressed_count)
    """
    if not baseline_keys:
        return violations, 0

    new = []
    suppressed = 0
    for v in violations:
        if _violation_key(v) in baseline_keys:
            suppressed += 1
        else:
            new.append(v)
    return new, suppressed
