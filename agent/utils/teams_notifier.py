"""Microsoft Teams report delivery via Power Automate webhooks."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:
    requests = None  # type: ignore[assignment]


def _grade(score_100: float) -> tuple:
    """Return (letter, label) from a 0-100 quality score."""
    if score_100 >= 90: return "A", "Excellent"
    if score_100 >= 80: return "B", "Good"
    if score_100 >= 70: return "C", "Average"
    if score_100 >= 60: return "D", "Below Avg"
    return "F", "Poor"


def build_flat_payload(
    projects_data: List[Dict[str, Any]],
    tl_name: str,
    tl_email: str,
    date_label: str,
    report_type: str = "daily",
    dashboard_url: str = "http://localhost:9090",
) -> Dict[str, Any]:
    """Build flat payload matching the existing Power Automate Parse JSON schema.

    Fields sent:
        project, week, target_email, total_commits, blocked_commits,
        success_rate, avg_score (0-10 scale), quality_grade,
        commit_trend, blocked_trend, improving,
        top_violations, developer_breakdown, top_performers,
        needs_coaching, action_items, critical_issues_list,
        developer_sections, issues_trend_summary
    """
    # ── aggregates ────────────────────────────────────────────────────────────
    project_names  = ", ".join(p.get("project_name", "") for p in projects_data) or "—"
    total_commits  = sum(p.get("total_commits", 0)   for p in projects_data)
    blocked        = sum(p.get("blocked_commits", 0) for p in projects_data)
    all_devs       = [d for p in projects_data for d in p.get("developers", [])]

    scores     = [p.get("quality_score", 0) for p in projects_data if p.get("quality_score")]
    avg_100    = round(sum(scores) / len(scores), 1) if scores else 0.0
    avg_10     = round(avg_100 / 10, 1)          # card template shows avg_score/10
    letter, lbl = _grade(avg_100)
    quality_grade = f"{letter} ({lbl})"

    success_pct = round((total_commits - blocked) / total_commits * 100, 1) if total_commits else 0.0
    success_rate = f"{success_pct}%"

    # ── severity counts from issue_details (tracker leaves counts at 0) ──────
    def _sev(details):
        c = sum(1 for i in details if i.get("severity") in ("critical", "error"))
        h = sum(1 for i in details if i.get("severity") in ("high", "warning"))
        m = sum(1 for i in details if i.get("severity") == "medium")
        l = sum(1 for i in details if i.get("severity") == "low")
        return c, h, m, l

    all_c = sum(_sev(d.get("issue_details", []))[0] for d in all_devs)
    all_h = sum(_sev(d.get("issue_details", []))[1] for d in all_devs)
    all_m = sum(_sev(d.get("issue_details", []))[2] for d in all_devs)
    all_l = sum(_sev(d.get("issue_details", []))[3] for d in all_devs)

    # ── improving / status ────────────────────────────────────────────────────
    if all_c > 0:
        improving = "🚨 Critical issues found"
    elif avg_100 >= 80:
        improving = "✅ Good standing"
    else:
        improving = "⚠️ Needs attention"

    # ── trends (stable unless historical data is available) ───────────────────
    commit_trend  = f"↑ {total_commits}"
    blocked_trend = f"↑ {blocked}" if blocked else "→ 0"

    # ── issues trend summary ─────────────────────────────────────────────────
    total_issues = sum(p.get("total_issues", 0) for p in projects_data)
    issues_trend_summary = (
        f"📋 Total Issues: {total_issues}\n\n"
        f"🔴 High: {all_c + all_h}\n"
        f"🟡 Medium: {all_m}\n"
        f"🟢 Low: {all_l}"
    )

    # ── top violations ────────────────────────────────────────────────────────
    rule_counts: Dict[str, int] = {}
    for d in all_devs:
        for i in d.get("issue_details", []):
            rule = i.get("title") or i.get("category") or "Unknown"
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
    top_violations = ", ".join(
        f"{r}: {n}" for r, n in
        sorted(rule_counts.items(), key=lambda x: -x[1])[:5]
    ) or "None"

    # ── developer sections (rich per-developer text for Teams TextBlock) ──────
    dev_section_lines: List[str] = []
    for project in projects_data:
        proj_name  = project.get("project_name", "Project")
        developers = project.get("developers", [])
        if not developers:
            continue

        dev_section_lines.append(f"━━━ {proj_name} ━━━")

        for dev in developers:
            name    = dev.get("name") or dev.get("email") or "Unknown"
            commits = dev.get("commits", 0)
            quality = dev.get("quality_score", 0) or 0
            score10 = round(quality / 10, 1)
            details = dev.get("issue_details", [])
            c, h, m, l = _sev(details)

            dev_section_lines.append(f"\n👤 {name}")

            # list critical/high issues individually
            top = [i for i in details
                   if i.get("severity") in ("critical", "error", "high", "warning")][:5]
            if top:
                for issue in top:
                    sev    = issue.get("severity", "high")
                    is_c   = sev in ("critical", "error")
                    emoji  = "🔴" if is_c else "🟠"
                    cat    = (issue.get("category") or "").lower()
                    ltype  = "[AI/HIGH]" if cat in ("security", "error_handling",
                                                     "performance", "maintainability") \
                             else "[RULES/ERROR]" if is_c else "[HIGH]"
                    fname  = issue.get("file", "")
                    fline  = issue.get("line", "")
                    msg    = issue.get("explanation", "")
                    rule   = issue.get("title", "")
                    loc    = f"{fname}:{fline}" if fline else fname

                    dev_section_lines.append(f"{emoji} {ltype}")
                    if loc or msg:
                        dev_section_lines.append(f"   📄 {loc} — {msg}" if loc else f"   {msg}")
                    if rule:
                        dev_section_lines.append(f"   Rule: {rule}")
            else:
                dev_section_lines.append("✅ No critical issues today")

            dev_section_lines.append(f"🟡 Medium: {m}  •  🟢 Low: {l}")
            blocked_str = f"Blocked: {c} ⚠️" if c else "Blocked: 0"
            dev_section_lines.append(f"📊 Commits: {commits}  •  {blocked_str}  •  Avg Score: {score10}/10")
            dev_section_lines.append("──────────────────────────")

    developer_sections = "\n".join(dev_section_lines)

    # ── developer breakdown (short summary line per dev) ─────────────────────
    breakdown_lines = []
    for d in all_devs[:10]:
        name    = d.get("name") or d.get("email") or "Unknown"
        commits = d.get("commits", 0)
        issues  = d.get("issues", 0)
        score   = round((d.get("quality_score") or 0) / 10, 1)
        breakdown_lines.append(f"{name}: {commits} commits, {issues} issues, {score}/10")
    developer_breakdown = "\n".join(breakdown_lines) or "No data"

    # ── top performers / needs coaching ──────────────────────────────────────
    sorted_devs = sorted(all_devs, key=lambda d: d.get("quality_score") or 0, reverse=True)
    top_performers = ", ".join(
        d.get("name") or d.get("email", "?")
        for d in sorted_devs[:3] if (d.get("quality_score") or 0) >= 70
    ) or "None"
    needs_coaching = ", ".join(
        d.get("name") or d.get("email", "?")
        for d in sorted_devs[::-1][:3] if (d.get("quality_score") or 0) < 60
    ) or "None"

    # ── critical issues list ──────────────────────────────────────────────────
    crit_lines: List[str] = []
    for d in all_devs:
        name = d.get("name") or d.get("email") or "Dev"
        for issue in d.get("issue_details", []):
            if issue.get("severity") in ("critical", "error"):
                title = issue.get("title", "Issue")
                fname = issue.get("file", "")
                crit_lines.append(f"{name} — {title} ({fname})")
                if len(crit_lines) >= 20:
                    break
        if len(crit_lines) >= 20:
            break
    critical_issues_list = "\n".join(crit_lines) or "No critical issues"

    # ── action items ─────────────────────────────────────────────────────────
    actions: List[str] = []
    if all_c > 0:
        actions.append(f"Fix {all_c} critical issue(s) immediately")
    if needs_coaching != "None":
        actions.append(f"Schedule coaching for: {needs_coaching}")
    if avg_100 < 70:
        actions.append("Team quality below 70% — review coding standards")
    if not actions:
        actions.append("Maintain current code quality standards")
    action_items = "\n".join(f"• {a}" for a in actions)

    return {
        "project":            project_names,
        "week":               date_label,
        "target_email":       tl_email,
        "total_commits":      total_commits,
        "blocked_commits":    blocked,
        "success_rate":       success_rate,
        "avg_score":          avg_10,           # 0-10 scale (card shows avg_score/10)
        "quality_grade":      quality_grade,
        "commit_trend":       commit_trend,
        "blocked_trend":      blocked_trend,
        "improving":          improving,
        "top_violations":     top_violations,
        "developer_breakdown": developer_breakdown,
        "top_performers":     top_performers,
        "needs_coaching":     needs_coaching,
        "action_items":       action_items,
        "critical_issues_list": critical_issues_list,
        "developer_sections": developer_sections,
        "issues_trend_summary": issues_trend_summary,
    }


def post_to_teams(webhook_url: str, payload: Dict[str, Any],
                  timeout: float = 15.0) -> Dict[str, Any]:
    """POST payload to a Power Automate webhook. Never raises."""
    if not webhook_url:
        return {"ok": False, "status": 0, "body": "empty webhook url"}
    if requests is None:
        return {"ok": False, "status": 0, "body": "requests library not installed"}
    try:
        r = requests.post(webhook_url, json=payload, timeout=timeout,
                          headers={"Content-Type": "application/json"})
        ok   = 200 <= r.status_code < 300
        body = (r.text or "")[:300]
        return {"ok": ok, "status": r.status_code, "body": body}
    except Exception as e:
        return {"ok": False, "status": 0, "body": f"{type(e).__name__}: {e}"}


# ── aliases so scheduler and any other callers stay consistent ───────────────
def build_report_card(*args, **kwargs):
    return build_flat_payload(*args, **kwargs)

def build_project_wise_report(*args, **kwargs):
    return build_flat_payload(*args, **kwargs)

def build_adaptive_card(*args, **kwargs):
    return build_flat_payload(*args, **kwargs)
