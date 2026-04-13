"""Local SQLite store — all data stays on this machine. No HTTP server needed.

Data is HMAC-signed on write and verified on read. Tampered rows are skipped.
Reviews are deleted after the daily report is sent.
HMAC key lives at ~/.cra/.key and is never stored in the database.
"""

import hashlib
import hmac
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

_CRA_DIR = Path.home() / ".cra"
_DB_PATH = _CRA_DIR / "reviews.db"
_KEY_PATH = _CRA_DIR / ".key"
_LAST_REPORT_PATH = _CRA_DIR / "last_report.json"

_IST = timezone(timedelta(hours=5, minutes=30))

# ── Power Automate webhook — read from env; falls back to hardcoded default ──
_FLOW_URL = os.getenv(
    "CRA_FLOW_URL",
    "https://defaultcff20d814abd4f219998f39afd1df6.2a.environment.api.powerplatform.com:443"
    "/powerautomate/automations/direct/workflows/243a9a7a866c46dca7f63ba89b2feced"
    "/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun"
    "&sv=1.0&sig=YOxpQhyv1jIB2Cc2UDF7bX4PEXz0BTKb0Nnl2Kw7_RI",
)


# ── HMAC Security ─────────────────────────────────────────────────────────────

def _get_secret_key() -> bytes:
    """Load or generate the HMAC secret key. Stored at ~/.cra/.key, never in DB."""
    _CRA_DIR.mkdir(parents=True, exist_ok=True)
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    key = os.urandom(32)
    _KEY_PATH.write_bytes(key)
    try:
        _KEY_PATH.chmod(0o600)  # owner read-only on Unix
    except OSError:
        pass  # Windows does not support chmod; file is in user's home dir
    return key


def _sign_review(row: dict) -> str:
    """Return HMAC-SHA256 hex digest over the immutable fields of a review row."""
    key = _get_secret_key()
    payload = (
        f"{row['developer_email']}|{row['project_key']}|{row['language']}|"
        f"{row['high_issues']}|{row['medium_issues']}|{row['low_issues']}|"
        f"{int(row['blocked'])}|{row['files_reviewed']}|{row['created_at']}"
    )
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_review(row: dict) -> bool:
    """Return True only when the stored signature matches a freshly computed one."""
    expected = _sign_review(row)
    stored = row.get("signature", "")
    return hmac.compare_digest(expected, stored)


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    project_key  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    tl_name      TEXT NOT NULL,
    tl_email     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    developer_email      TEXT    NOT NULL,
    project_key          TEXT    NOT NULL,
    language             TEXT    NOT NULL,
    framework            TEXT,
    quality_score        REAL,
    high_issues          INTEGER DEFAULT 0,
    medium_issues        INTEGER DEFAULT 0,
    low_issues           INTEGER DEFAULT 0,
    blocked              INTEGER DEFAULT 0,
    files_reviewed       INTEGER DEFAULT 0,
    security_issues      INTEGER DEFAULT 0,
    quality_issues       INTEGER DEFAULT 0,
    style_issues         INTEGER DEFAULT 0,
    performance_issues   INTEGER DEFAULT 0,
    critical_issues_json TEXT,
    created_at           TEXT    NOT NULL,
    signature            TEXT    NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    _CRA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create DB tables if they do not exist."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def save_project(name: str, tl_name: str, tl_email: str) -> str:
    """Create a new project in the local DB and return its project_key."""
    import secrets
    init_db()
    project_key = secrets.token_hex(8)
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO project VALUES (?,?,?,?,?)",
            (project_key, name, tl_name, tl_email,
             datetime.now(_IST).isoformat()),
        )
    return project_key


def save_project_from_config(config: dict) -> str:
    """Import a project from a cra-project.json config dict into the local DB.

    The config must contain: project_key, name, tl_name, tl_email.
    Uses INSERT OR REPLACE so re-running is safe.
    Returns the project_key.
    """
    init_db()
    project_key = config["project_key"]
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project VALUES (?,?,?,?,?)",
            (
                project_key,
                config["name"],
                config["tl_name"],
                config["tl_email"],
                config.get("created_at", datetime.now(_IST).isoformat()),
            ),
        )
    return project_key


def save_developer(name: str, email: str, project_key: str) -> Optional[Dict]:
    """Verify the project_key exists locally and return project info.

    The developer table has been removed — identity is read from
    ~/.cra/config.json.  This function is kept so hook_installer.py
    can confirm the project key is valid after cra-project.json is loaded.
    """
    project = get_project(project_key)
    if not project:
        return None
    return {"project": project["name"], "tl": project["tl_name"]}


def save_review(
    developer_email: str,
    project_key: str,
    language: str,
    framework: str,
    quality_score: Optional[float],
    high_issues: int,
    medium_issues: int,
    low_issues: int,
    blocked: bool,
    files_reviewed: int,
    security_issues: int,
    quality_issues: int,
    style_issues: int,
    performance_issues: int,
    critical_issues: Optional[list],
) -> None:
    """Write a review row to local SQLite with an HMAC signature.

    Silently swallows all exceptions so a DB failure never blocks a commit.
    """
    try:
        init_db()
        created_at = datetime.now(_IST).isoformat()
        row_for_signing = {
            "developer_email": developer_email,
            "project_key": project_key,
            "language": language,
            "high_issues": high_issues,
            "medium_issues": medium_issues,
            "low_issues": low_issues,
            "blocked": int(blocked),
            "files_reviewed": files_reviewed,
            "created_at": created_at,
        }
        signature = _sign_review(row_for_signing)
        with _connect() as conn:
            conn.execute(
                """INSERT INTO review
                   (developer_email, project_key, language, framework, quality_score,
                    high_issues, medium_issues, low_issues, blocked, files_reviewed,
                    security_issues, quality_issues, style_issues, performance_issues,
                    critical_issues_json, created_at, signature)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (developer_email, project_key, language, framework, quality_score,
                 high_issues, medium_issues, low_issues, int(blocked), files_reviewed,
                 security_issues, quality_issues, style_issues, performance_issues,
                 json.dumps(critical_issues) if critical_issues else None,
                 created_at, signature),
            )
    except Exception:
        pass  # never block a commit because of a store failure


def get_project(project_key: str) -> Optional[Dict]:
    """Return project row as dict, or None if not found."""
    try:
        init_db()
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM project WHERE project_key=?", (project_key,)
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _get_developer_name(developer_email: str) -> str:
    """Read the developer's display name from ~/.cra/config.json."""
    try:
        config_path = Path.home() / ".cra" / "config.json"
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            name = data.get("developer_name", "").strip()
            if name:
                return name
    except Exception:
        pass
    return developer_email  # fallback to email if name not found


def delete_reviews(project_key: str, developer_email: str) -> int:
    """Delete this developer's reviews for a project after the report is sent."""
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM review WHERE project_key=? AND developer_email=?",
                (project_key, developer_email),
            )
            return cur.rowcount
    except Exception:
        return 0


# ── Report Builder ────────────────────────────────────────────────────────────

def _build_report(project_key: str, developer_email: str, period: str = "daily") -> Optional[Dict]:
    """Aggregate this developer's review data and return the report payload dict.

    Scoped to a single developer_email — each machine sends only its own report.
    Rows whose HMAC signature does not verify are silently skipped and flagged.
    """
    now = datetime.now(_IST)

    if period == "daily":
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = period_start - timedelta(days=1)
        prev_end = period_start
        period_label = now.strftime("%A, %d %b %Y")
    else:
        period_start = now - timedelta(days=7)
        prev_start = now - timedelta(days=14)
        prev_end = period_start
        period_label = (
            f"{(now - timedelta(days=7)).strftime('%d %b')} \u2013 {now.strftime('%d %b %Y')}"
        )

    try:
        with _connect() as conn:
            raw_current = conn.execute(
                "SELECT * FROM review WHERE project_key=? AND developer_email=? AND created_at>=?",
                (project_key, developer_email, period_start.isoformat()),
            ).fetchall()
            raw_prev = conn.execute(
                "SELECT * FROM review WHERE project_key=? AND developer_email=? AND created_at>=? AND created_at<?",
                (project_key, developer_email, prev_start.isoformat(), prev_end.isoformat()),
            ).fetchall()
    except Exception:
        return None

    # Verify HMAC on every current-period row; skip tampered ones
    tampered = 0
    reviews: List[Dict] = []
    for row in raw_current:
        d = dict(row)
        if _verify_review(d):
            reviews.append(d)
        else:
            tampered += 1

    if tampered:
        print(
            f"[SECURITY WARNING] {tampered} review row(s) failed HMAC verification "
            "and were excluded from the report."
        )

    if not reviews:
        return None

    prev_reviews = [dict(r) for r in raw_prev]
    developer_name = _get_developer_name(developer_email)

    # ── Overall Stats ──────────────────────────────────────────────────────────
    total_commits = len(reviews)
    blocked_count = sum(1 for r in reviews if r["blocked"])
    scores = [r["quality_score"] for r in reviews if r["quality_score"] is not None]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    total_high   = sum(r["high_issues"]   for r in reviews)
    total_medium = sum(r["medium_issues"] for r in reviews)
    total_low    = sum(r["low_issues"]    for r in reviews)
    success_rate = (
        round(((total_commits - blocked_count) / total_commits) * 100, 1)
        if total_commits else 0
    )

    if avg_score >= 9:   grade = "A (Excellent)"
    elif avg_score >= 8: grade = "B (Good)"
    elif avg_score >= 7: grade = "C (Average)"
    else:                grade = "D (Needs Improvement)"

    # ── Trends ────────────────────────────────────────────────────────────────
    def _trend(curr: int, prev: int) -> str:
        d = curr - prev
        return f"↑ {d}" if d > 0 else (f"↓ {abs(d)}" if d < 0 else "→ 0")

    last_commits = len(prev_reviews)
    last_blocked = sum(1 for r in prev_reviews if r["blocked"])
    prev_high    = sum(r["high_issues"]   for r in prev_reviews)
    prev_medium  = sum(r["medium_issues"] for r in prev_reviews)
    prev_low     = sum(r["low_issues"]    for r in prev_reviews)
    curr_total   = total_high + total_medium + total_low
    prev_total_i = prev_high + prev_medium + prev_low
    total_change = curr_total - prev_total_i

    commits_change = total_commits - last_commits
    blocked_change = blocked_count - last_blocked
    improving      = blocked_change < 0

    period_label_short = "yesterday" if period == "daily" else "last week"
    total_trend_icon = "📉" if total_change < 0 else ("📈" if total_change > 0 else "➡️")
    issues_trend_summary = (
        f"{total_trend_icon} Total Issues: {curr_total}  ({_trend(curr_total, prev_total_i)} vs {period_label_short})\n\n"
        f"🔴 High: {total_high}  ({_trend(total_high, prev_high)} vs {period_label_short})\n\n"
        f"🟡 Medium: {total_medium}  ({_trend(total_medium, prev_medium)} vs {period_label_short})\n\n"
        f"🟢 Low: {total_low}  ({_trend(total_low, prev_low)} vs {period_label_short})"
    )

    # ── Top Violations ─────────────────────────────────────────────────────────
    violation_counts: Counter = Counter()
    for r in reviews:
        violation_counts["Security"]     += r["security_issues"]
        violation_counts["Code Quality"] += r["quality_issues"]
        violation_counts["Style"]        += r["style_issues"]
        violation_counts["Performance"]  += r["performance_issues"]

    _emoji = {"Security": "🔴", "Code Quality": "🟡", "Style": "🟢", "Performance": "🟠"}
    top_violations_str = "\n\n".join(
        f"{_emoji.get(cat, '⚫')} {cat}: {cnt} violations"
        for cat, cnt in violation_counts.most_common(3) if cnt > 0
    ) or "✅ No violations today!"

    # ── Developer Stats (single developer) ────────────────────────────────────
    dev_commits       = total_commits
    dev_blocked       = blocked_count
    dev_scores        = scores
    dev_medium_issues = total_medium
    dev_low_issues    = total_low
    dev_score         = avg_score
    dev_warn          = " ⚠️" if dev_blocked > 2 else ""
    developer_breakdown = (
        f"👤 {developer_name}\n"
        f"   Score: {dev_score}/10  •  Commits: {dev_commits}  •  Blocked: {dev_blocked}{dev_warn}"
    )

    # ── Recommendations ────────────────────────────────────────────────────────
    recs = []
    if total_high > 10:
        recs.append("⚠️ High issues detected — review and fix before next push")
    if blocked_count > total_commits * 0.3:
        recs.append("📚 30%+ commits blocked — review coding standards")
    if avg_score < 7:
        recs.append("🎯 Low quality score — focus on code quality improvements")
    if violation_counts["Security"] > 5:
        recs.append("🔒 Security violations high — review OWASP best practices")
    if dev_blocked > 3:
        recs.append(f"👥 Multiple blocked commits — 1-on-1 coaching recommended for {developer_name}")
    if improving and blocked_count == 0:
        recs.append("🎉 Perfect day — zero blocked commits! Keep it up!")
    action_items_str = "\n".join(recs) if recs else "✅ Keep up the good work!"

    # ── Critical Issues List ───────────────────────────────────────────────────
    all_critical: List[Dict] = []
    for r in reviews:
        if not r.get("critical_issues_json"):
            continue
        try:
            issues = json.loads(r["critical_issues_json"])
            for issue in issues:
                issue["developer"] = developer_name
                all_critical.append(issue)
        except Exception:
            pass

    _sev_rank = {"error": 0, "high": 0, "warning": 1, "medium": 1, "low": 2, "info": 2}
    all_critical.sort(key=lambda x: _sev_rank.get(x.get("severity", "low"), 2))

    formatted_issues = []
    for issue in all_critical[:10]:
        sev  = issue.get("severity", "").upper()
        src  = issue.get("source", "?").upper()
        loc  = (
            f"{issue.get('file', '')}:{issue.get('line')}"
            if issue.get("line") else issue.get("file", "")
        )
        rule_tag = issue.get("rule_id") or issue.get("category", "")
        icon = "🔴" if sev in ("ERROR", "HIGH") else "🟡"
        formatted_issues.append(
            f"{icon} [{src}/{sev}] {issue.get('developer', '')}\n"
            f"   📄 {loc}  —  {issue.get('message', '')}" +
            (f"\n   Rule: {rule_tag}" if rule_tag else "")
        )
    critical_issues_list_str = (
        "\n\n".join(formatted_issues) if formatted_issues
        else "✅ No critical issues today!"
    )

    # ── Developer Section (single developer) ──────────────────────────────────
    if all_critical:
        issue_lines = []
        for issue in all_critical[:10]:
            sev      = issue.get("severity", "").upper()
            src      = issue.get("source", "?").upper()
            loc      = (
                f"{issue.get('file', '')}:{issue.get('line')}"
                if issue.get("line") else issue.get("file", "")
            )
            rule_tag = issue.get("rule_id") or issue.get("category", "")
            icon     = "🔴" if sev in ("ERROR", "HIGH") else "🟡"
            issue_lines.append(
                f"{icon} [{src}/{sev}]\n   📄 {loc}  —  {issue.get('message', '')}" +
                (f"\n   Rule: {rule_tag}" if rule_tag else "")
            )
        dev_issues_str = "\n\n".join(issue_lines)
    else:
        dev_issues_str = "✅ No critical issues today"

    developer_sections = (
        f"👤 {developer_name}\n\n{dev_issues_str}\n\n"
        f"🟡 Medium: {dev_medium_issues}  •  🟢 Low: {dev_low_issues}\n\n"
        f"📊 Commits: {dev_commits}  •  Blocked: {dev_blocked}{dev_warn}  •  Avg Score: {dev_score}/10"
    )

    return {
        "project_key":          project_key,
        "developer_name":       developer_name,
        "developer_email":      developer_email,
        "week":                 period_label,
        "period":               period,
        "total_commits":        total_commits,
        "blocked_commits":      blocked_count,
        "success_rate":         f"{success_rate}%",
        "avg_score":            avg_score,
        "quality_grade":        grade,
        "commit_trend":         (
            f"↑ {commits_change}" if commits_change > 0
            else (f"↓ {abs(commits_change)}" if commits_change < 0 else "→ 0")
        ),
        "blocked_trend":        (
            f"↓ {abs(blocked_change)}" if blocked_change < 0
            else (f"↑ {blocked_change}" if blocked_change > 0 else "→ 0")
        ),
        "improving":            "✅ Improving!" if improving else "⚠️ Needs attention",
        "top_violations":       top_violations_str,
        "issues_trend_summary": issues_trend_summary,
        "developer_breakdown":  developer_breakdown,
        "top_performers":       developer_name if avg_score >= 8.5 and blocked_count == 0 else "None today",
        "needs_coaching":       f"• {developer_name} (Score: {avg_score}, {blocked_count} blocked)" if avg_score < 7 or blocked_count > 3 else "None",
        "action_items":         action_items_str,
        "critical_issues_list": critical_issues_list_str,
        "developer_sections":   developer_sections,
    }


# ── Daily Report Trigger ──────────────────────────────────────────────────────

def check_and_send_report(project_key: str, developer_email: str) -> None:
    """Called after every push. Sends this developer's daily report if it is
    past 6:30 PM IST and has not already been sent today.
    Deletes only this developer's reviews after sending.

    Silently swallows all exceptions so a report failure never blocks a commit.
    """
    try:
        project = get_project(project_key)
        if not project:
            return

        if not _FLOW_URL:
            return  # no flow URL configured — skip silently

        now_ist = datetime.now(_IST)
        today_str = now_ist.strftime("%Y-%m-%d")

        # Only fire after 18:30 IST (6:30 PM)
        if now_ist.hour < 18 or (now_ist.hour == 18 and now_ist.minute < 30):
            return

        # Dedup key is per-developer per-day so each machine sends independently
        dedup_key = f"{project_key}:{developer_email}"
        last_sent: Dict = {}
        if _LAST_REPORT_PATH.exists():
            try:
                last_sent = json.loads(_LAST_REPORT_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass

        if last_sent.get(dedup_key) == today_str:
            return  # already sent today for this developer

        report = _build_report(project_key, developer_email)
        if not report:
            return  # no data to report today

        payload = {
            "project":              project["name"],
            "developer_name":       report["developer_name"],
            "week":                 report["week"],
            "target_email":         project["tl_email"],
            "total_commits":        report["total_commits"],
            "blocked_commits":      report["blocked_commits"],
            "success_rate":         report["success_rate"],
            "avg_score":            report["avg_score"],
            "quality_grade":        report["quality_grade"],
            "commit_trend":         report["commit_trend"],
            "blocked_trend":        report["blocked_trend"],
            "improving":            report["improving"],
            "top_violations":       report["top_violations"],
            "issues_trend_summary": report["issues_trend_summary"],
            "developer_breakdown":  report["developer_breakdown"],
            "top_performers":       report["top_performers"],
            "needs_coaching":       report["needs_coaching"],
            "action_items":         report["action_items"],
            "critical_issues_list": report["critical_issues_list"],
            "developer_sections":   report["developer_sections"],
        }

        import requests
        response = requests.post(_FLOW_URL, json=payload, timeout=10)

        if response.status_code in (200, 202):
            last_sent[dedup_key] = today_str
            _LAST_REPORT_PATH.write_text(json.dumps(last_sent), encoding="utf-8")
            deleted = delete_reviews(project_key, developer_email)
            print(
                f"[CRA] Daily report sent for '{report['developer_name']}' "
                f"({project['name']}) — {deleted} record(s) cleaned up."
            )
        else:
            print(f"[CRA] Report delivery failed: HTTP {response.status_code}")

    except Exception:
        pass  # never block a commit because of a report failure
