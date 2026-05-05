"""Microsoft Teams report delivery via Power Automate webhooks."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:
    requests = None  # type: ignore[assignment]



def build_flat_payload(
    projects_data: List[Dict[str, Any]],
    tl_name: str,
    tl_email: str,
    date_label: str,
    report_type: str = "daily",
    dashboard_url: str = "http://localhost:9090",
) -> Dict[str, Any]:
    """Build flat payload matching the Power Automate Parse JSON schema.

    Uses the developer_reviews-based data from get_tl_report_data:
    one row per push → accurate commit/blocked counts, category breakdowns,
    and trend deltas vs the previous period. Format is identical to the
    local SQLite report.

    Fields sent (unchanged PA schema):
        project, week, target_email, total_commits, blocked_commits,
        success_rate, avg_score (0-10), quality_grade,
        commit_trend, blocked_trend, improving,
        top_violations, developer_breakdown, top_performers,
        needs_coaching, action_items, critical_issues_list,
        developer_sections, issues_trend_summary
    """
    all_devs      = [d for p in projects_data for d in p.get("developers", [])]
    project_names = ", ".join(p.get("project_name", "") for p in projects_data) or "—"
    period_label  = "yesterday" if report_type == "daily" else "last week"

    # ── TL-level totals ───────────────────────────────────────────────────────
    total_commits  = sum(d.get("total_commits",      0) for d in all_devs)
    blocked        = sum(d.get("blocked_commits",    0) for d in all_devs)
    all_high       = sum(d.get("high_issues",        0) for d in all_devs)
    all_medium     = sum(d.get("medium_issues",      0) for d in all_devs)
    all_low        = sum(d.get("low_issues",         0) for d in all_devs)
    all_security   = sum(d.get("security_issues",    0) for d in all_devs)
    all_quality    = sum(d.get("quality_issues",     0) for d in all_devs)
    all_style      = sum(d.get("style_issues",       0) for d in all_devs)
    all_perf       = sum(d.get("performance_issues", 0) for d in all_devs)
    total_issues   = all_high + all_medium + all_low

    # ── quality / grade (avg_score already 0-10 from developer_reviews) ──────
    scores    = [d.get("avg_score", 0) for d in all_devs if d.get("avg_score")]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    if avg_score >= 9:   quality_grade = "A (Excellent)"
    elif avg_score >= 8: quality_grade = "B (Good)"
    elif avg_score >= 7: quality_grade = "C (Average)"
    else:                quality_grade = "D (Needs Improvement)"

    success_pct  = round((total_commits - blocked) / total_commits * 100, 1) if total_commits else 0.0
    success_rate = "%s%%" % success_pct

    # ── trend helper ──────────────────────────────────────────────────────────
    def _tr(n):
        if n > 0: return "↑ %s" % n
        if n < 0: return "↓ %s" % abs(n)
        return "→ 0"

    def _delta(n, unit=""):
        if n > 0:  return "(+%s%s vs prev)" % (n, unit)
        if n < 0:  return "(%s%s vs prev)"  % (n, unit)
        return ""

    commits_delta = sum(d.get("commits_delta", 0) for d in all_devs)
    blocked_delta = sum(d.get("blocked_delta", 0) for d in all_devs)
    high_delta    = sum(d.get("high_delta",    0) for d in all_devs)
    medium_delta  = sum(d.get("medium_delta",  0) for d in all_devs)
    low_delta     = sum(d.get("low_delta",     0) for d in all_devs)
    total_delta   = high_delta + medium_delta + low_delta

    commit_trend  = _tr(commits_delta)
    blocked_trend = _tr(blocked_delta)

    # ── improving ─────────────────────────────────────────────────────────────
    if blocked_delta < 0:
        improving = "✅ Improving!"
    elif all_high > 0:
        improving = "🚨 Critical issues found"
    elif avg_score >= 8:
        improving = "✅ Good standing"
    else:
        improving = "⚠️ Needs attention"

    # ── top violations (category-based, matches local format) ─────────────────
    _cat_emoji = {"Security": "🔴", "Code Quality": "🟡", "Style": "🟢", "Performance": "🟠"}
    cat_counts = {
        "Security":     all_security,
        "Code Quality": all_quality,
        "Style":        all_style,
        "Performance":  all_perf,
    }
    top_violations = "\n\n".join(
        "%s %s: %s violations" % (_cat_emoji.get(cat, "⚫"), cat, cnt)
        for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1])[:3] if cnt > 0
    ) or "✅ No violations today!"

    # ── issues trend summary (matches local format) ───────────────────────────
    trend_icon = "📉" if total_delta < 0 else ("📈" if total_delta > 0 else "➡️")
    issues_trend_summary = (
        "%s Total Issues: %s  (%s vs %s)\n\n"
        "🔴 High: %s  (%s vs %s)\n\n"
        "🟡 Medium: %s  (%s vs %s)\n\n"
        "🟢 Low: %s  (%s vs %s)"
    ) % (
        trend_icon, total_issues, _tr(total_delta),  period_label,
        all_high,   _tr(high_delta),   period_label,
        all_medium, _tr(medium_delta), period_label,
        all_low,    _tr(low_delta),    period_label,
    )

    # ── developer sections (one block per dev, identical to local format) ─────
    dev_section_lines: List[str] = []
    for project in projects_data:
        proj_name  = project.get("project_name", "Project")
        developers = project.get("developers", [])
        if not developers:
            continue
        dev_section_lines.append("━━━ %s ━━━" % proj_name)

        for dev in developers:
            name    = dev.get("name") or dev.get("email") or "Unknown"
            commits = dev.get("total_commits",   0)
            bk      = dev.get("blocked_commits", 0)
            score   = dev.get("avg_score",       0) or 0
            medium  = dev.get("medium_issues",   0)
            low     = dev.get("low_issues",      0)
            cd      = dev.get("commits_delta",   0)
            bd      = dev.get("blocked_delta",   0)
            sd      = dev.get("score_delta",     0.0)
            hd      = dev.get("high_delta",      0)
            md_d    = dev.get("medium_delta",    0)
            ld      = dev.get("low_delta",       0)

            dev_warn  = " ⚠️" if bk > 2 else ""
            c_str     = _delta(cd)
            i_str     = _delta(hd + md_d + ld)
            q_str     = _delta(round(sd, 1))

            dev_section_lines.append("\n👤 %s" % name)

            # critical / high issues with file:line
            critical_list = dev.get("critical_issues", [])
            top = [i for i in critical_list
                   if i.get("severity") in ("error", "critical", "high", "warning")][:5]
            if top:
                for issue in top:
                    sev   = issue.get("severity", "high")
                    is_c  = sev in ("error", "critical")
                    emoji = "🔴" if is_c else "🟠"
                    cat   = (issue.get("category") or "").lower()
                    ltype = "[AI/HIGH]" if cat in ("security", "error_handling",
                                                    "performance", "maintainability") \
                            else "[RULES/ERROR]" if is_c else "[HIGH]"
                    fname = issue.get("file", "")
                    fline = issue.get("line", "")
                    msg   = issue.get("message", "") or issue.get("explanation", "")
                    rule  = issue.get("rule_id") or issue.get("title") or issue.get("category", "")
                    loc   = "%s:%s" % (fname, fline) if fline else fname
                    dev_section_lines.append("%s %s" % (emoji, ltype))
                    if loc or msg:
                        dev_section_lines.append(
                            "   📄 %s — %s" % (loc, msg) if loc else "   %s" % msg
                        )
                    if rule:
                        dev_section_lines.append("   Rule: %s" % rule)
            else:
                dev_section_lines.append("✅ No critical issues today")

            bk_str = ("Blocked: %s ⚠️" % bk) if bk else "Blocked: 0"
            dev_section_lines.append("🟡 Medium: %s  •  🟢 Low: %s%s" % (medium, low, i_str))
            dev_section_lines.append(
                "📊 Commits: %s%s  •  %s  •  Avg Score: %s/10%s%s"
                % (commits, c_str, bk_str, score, q_str, dev_warn)
            )
            dev_section_lines.append("──────────────────────────")

    developer_sections = "\n".join(dev_section_lines)

    # ── developer breakdown (short summary per dev, matches local format) ─────
    breakdown_lines: List[str] = []
    for d in all_devs[:10]:
        name  = d.get("name") or d.get("email") or "Unknown"
        c     = d.get("total_commits",   0)
        bk    = d.get("blocked_commits", 0)
        score = d.get("avg_score",       0) or 0
        warn  = " ⚠️" if bk > 2 else ""
        breakdown_lines.append(
            "👤 %s\n   Score: %s/10  •  Commits: %s  •  Blocked: %s%s"
            % (name, score, c, bk, warn)
        )
    developer_breakdown = "\n\n".join(breakdown_lines) or "No data"

    # ── top performers / needs coaching ──────────────────────────────────────
    sorted_devs = sorted(all_devs, key=lambda d: d.get("avg_score") or 0, reverse=True)
    top_performers = ", ".join(
        d.get("name") or d.get("email", "?")
        for d in sorted_devs[:3]
        if (d.get("avg_score") or 0) >= 8.5 and d.get("blocked_commits", 0) == 0
    ) or "None today"
    needs_coaching = ", ".join(
        d.get("name") or d.get("email", "?")
        for d in sorted_devs[::-1][:3]
        if (d.get("avg_score") or 0) < 7 or d.get("blocked_commits", 0) > 3
    ) or "None"

    # ── critical issues list (top 20, matches local format) ──────────────────
    crit_lines: List[str] = []
    for d in all_devs:
        name = d.get("name") or d.get("email") or "Dev"
        for issue in d.get("critical_issues", []):
            if issue.get("severity") in ("error", "critical"):
                sev      = issue.get("severity", "").upper()
                src      = issue.get("source", "rule").upper()
                loc      = ("%s:%s" % (issue.get("file", ""), issue.get("line"))
                            if issue.get("line") else issue.get("file", ""))
                rule_tag = issue.get("rule_id") or issue.get("category", "")
                msg      = issue.get("message", "") or issue.get("explanation", "")
                crit_lines.append(
                    "🔴 [%s/%s] %s\n   📄 %s  —  %s%s"
                    % (src, sev, name, loc, msg,
                       "\n   Rule: %s" % rule_tag if rule_tag else "")
                )
                if len(crit_lines) >= 20:
                    break
        if len(crit_lines) >= 20:
            break
    critical_issues_list = "\n\n".join(crit_lines) or "✅ No critical issues today!"

    # ── action items ──────────────────────────────────────────────────────────
    actions: List[str] = []
    if all_high > 0:
        actions.append("Fix %s high-severity issue(s) immediately" % all_high)
    if needs_coaching != "None":
        actions.append("Schedule coaching for: %s" % needs_coaching)
    if avg_score < 7:
        actions.append("Team quality below 7/10 — review coding standards")
    if not actions:
        actions.append("Maintain current code quality standards")
    action_items = "\n".join("• %s" % a for a in actions)

    return {
        "project":             project_names,
        "week":                date_label,
        "target_email":        tl_email,
        "total_commits":       total_commits,
        "blocked_commits":     blocked,
        "success_rate":        success_rate,
        "avg_score":           avg_score,
        "quality_grade":       quality_grade,
        "commit_trend":        commit_trend,
        "blocked_trend":       blocked_trend,
        "improving":           improving,
        "top_violations":      top_violations,
        "developer_breakdown": developer_breakdown,
        "top_performers":      top_performers,
        "needs_coaching":      needs_coaching,
        "action_items":        action_items,
        "critical_issues_list": critical_issues_list,
        "developer_sections":  developer_sections,
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
