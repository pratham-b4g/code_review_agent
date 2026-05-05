"""Microsoft Teams report delivery via Power Automate webhooks."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:
    requests = None  # type: ignore[assignment]


def _grade(score: float) -> tuple:
    """Return (letter, label, AC-color) from a 0-100 quality score."""
    if score >= 90: return "A", "Excellent", "Good"
    if score >= 80: return "B", "Good",      "Good"
    if score >= 70: return "C", "Average",   "Warning"
    if score >= 60: return "D", "Below Avg", "Warning"
    return "F", "Poor", "Attention"


def build_report_card(
    projects_data: List[Dict[str, Any]],
    tl_name: str,
    tl_email: str,
    date_label: str,
    report_type: str = "daily",
    dashboard_url: str = "http://localhost:9090",
) -> Dict[str, Any]:
    """Build an Adaptive Card payload that matches the screenshot report format.

    Returns {"target_email": tl_email, "card": {...}}.

    Power Automate Parse JSON schema must be:
        { "type": "object", "properties": {
            "target_email": {"type": "string"},
            "card": {"type": "object"}
          }
        }
    Post-card step: Recipient = body('Parse_JSON')?['target_email']
                    Card      = string(body('Parse_JSON')?['card'])
    """
    body: List[Dict] = []

    # ── aggregate totals ──────────────────────────────────────────────────────
    total_commits  = sum(p.get("total_commits", 0)  for p in projects_data)
    total_issues   = sum(p.get("total_issues", 0)   for p in projects_data)
    blocked        = sum(p.get("blocked_commits", 0) for p in projects_data)
    scores         = [p.get("quality_score", 0) for p in projects_data if p.get("quality_score")]
    avg_score      = round(sum(scores) / len(scores), 1) if scores else 0.0
    score_10       = round(avg_score / 10, 1)
    success_pct    = round((total_commits - blocked) / total_commits * 100, 1) if total_commits else 0.0
    letter, label, _ = _grade(avg_score)

    all_devs = [d for p in projects_data for d in p.get("developers", [])]
    # recompute severity counts from issue_details (tracker leaves counts at 0)
    def _counts(dev):
        details = dev.get("issue_details", [])
        return (
            sum(1 for i in details if i.get("severity") in ("critical", "error")),
            sum(1 for i in details if i.get("severity") in ("high", "warning")),
            sum(1 for i in details if i.get("severity") == "medium"),
            sum(1 for i in details if i.get("severity") == "low"),
        )

    all_critical = sum(_counts(d)[0] for d in all_devs)
    all_high     = sum(_counts(d)[1] for d in all_devs)
    all_medium   = sum(_counts(d)[2] for d in all_devs)
    all_low      = sum(_counts(d)[3] for d in all_devs)

    if all_critical > 0:
        status_text, status_color = "🚨 Critical issues found", "Attention"
    elif avg_score >= 80:
        status_text, status_color = "✅ Good standing",        "Good"
    else:
        status_text, status_color = "⚠️ Needs attention",      "Warning"

    title_prefix = {
        "daily":   "📊 Daily",
        "weekly":  "📊 Weekly",
        "monthly": "📊 Monthly",
    }.get(report_type, "📊")
    project_names = ", ".join(p.get("project_name", "") for p in projects_data) or "—"

    # ── header ────────────────────────────────────────────────────────────────
    body.append({
        "type": "Container",
        "style": "emphasis",
        "bleed": True,
        "items": [
            {
                "type": "TextBlock",
                "text": f"{title_prefix} Code Review Report",
                "weight": "Bolder",
                "size": "Large",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Project:", "value": project_names},
                    {"title": "Date:",    "value": date_label},
                    {"title": "Grade:",   "value": f"{letter} ({label})"},
                    {"title": "Status:",  "value": status_text},
                ],
            },
        ],
    })

    # ── stats row ─────────────────────────────────────────────────────────────
    score_color = "Good" if avg_score >= 80 else ("Warning" if avg_score >= 60 else "Attention")
    body.append({
        "type": "ColumnSet",
        "spacing": "Medium",
        "columns": [
            {
                "type": "Column", "width": "1",
                "items": [
                    {"type": "TextBlock", "text": "SUCCESS RATE", "size": "Small",
                     "isSubtle": True, "weight": "Bolder"},
                    {"type": "TextBlock", "text": f"{success_pct}%",
                     "size": "ExtraLarge", "weight": "Bolder",
                     "color": "Good" if success_pct >= 80 else "Warning"},
                    {"type": "TextBlock", "text": f"↑ {total_commits} commits",
                     "size": "Small", "isSubtle": True},
                ],
            },
            {
                "type": "Column", "width": "1",
                "items": [
                    {"type": "TextBlock", "text": "BLOCKED", "size": "Small",
                     "isSubtle": True, "weight": "Bolder"},
                    {"type": "TextBlock", "text": str(blocked),
                     "size": "ExtraLarge", "weight": "Bolder",
                     "color": "Attention" if blocked > 0 else "Good"},
                    {"type": "TextBlock",
                     "text": f"↑ {blocked} vs yesterday" if blocked else "None today",
                     "size": "Small", "isSubtle": True},
                ],
            },
            {
                "type": "Column", "width": "1",
                "items": [
                    {"type": "TextBlock", "text": "AVG SCORE", "size": "Small",
                     "isSubtle": True, "weight": "Bolder"},
                    {"type": "TextBlock", "text": f"{score_10}/10",
                     "size": "ExtraLarge", "weight": "Bolder", "color": score_color},
                    {"type": "TextBlock", "text": f"{letter} ({label})",
                     "size": "Small", "isSubtle": True},
                ],
            },
        ],
    })

    # ── issue trend ───────────────────────────────────────────────────────────
    body.append({
        "type": "Container",
        "style": "default",
        "spacing": "Medium",
        "items": [
            {"type": "TextBlock", "text": "📈 Issue Trend vs Yesterday",
             "weight": "Bolder", "size": "Medium"},
            {"type": "TextBlock",
             "text": f"📋 Total Issues: {total_issues}",
             "wrap": True, "spacing": "Small"},
            {"type": "TextBlock",
             "text": (f"🔴 High: {all_critical + all_high}   "
                      f"🟡 Medium: {all_medium}   "
                      f"🟢 Low: {all_low}"),
             "wrap": True, "spacing": "None"},
        ],
    })

    # ── per-project developer sections ────────────────────────────────────────
    for project in projects_data:
        proj_name  = project.get("project_name", "Project")
        developers = project.get("developers", [])
        if not developers:
            continue

        body.append({
            "type": "TextBlock",
            "text": f"👥 Developer Report — {proj_name}",
            "weight": "Bolder",
            "size": "Medium",
            "spacing": "Large",
            "separator": True,
        })

        for dev in developers:
            name    = dev.get("name") or dev.get("email") or "Unknown"
            commits = dev.get("commits", 0)
            quality = dev.get("quality_score", 0) or 0
            score10 = round(quality / 10, 1)
            details = dev.get("issue_details", [])

            crit_cnt, high_cnt, med_cnt, low_cnt = _counts(dev)

            # developer name header
            body.append({
                "type": "TextBlock",
                "text": f"👤 {name}",
                "weight": "Bolder",
                "size": "Small",
                "spacing": "Medium",
            })

            # list critical + high issues individually
            top_issues = [i for i in details
                          if i.get("severity") in ("critical", "error", "high", "warning")][:5]

            if top_issues:
                for issue in top_issues:
                    sev = issue.get("severity", "high")
                    is_crit = sev in ("critical", "error")
                    emoji   = "🔴" if is_crit else "🟠"
                    cat     = (issue.get("category") or "").lower()
                    label_t = "[AI/HIGH]" if cat in ("security", "error_handling",
                                                      "performance", "maintainability") \
                              else "[RULES/ERROR]" if is_crit else "[HIGH]"
                    f_name  = issue.get("file", "")
                    f_line  = issue.get("line", "")
                    msg     = issue.get("explanation", "")
                    rule    = issue.get("title", "")
                    loc     = f"{f_name}:{f_line}" if f_line else f_name

                    body.append({
                        "type": "TextBlock",
                        "text": f"{emoji} {label_t}",
                        "size": "Small",
                        "weight": "Bolder",
                        "color": "Attention" if is_crit else "Warning",
                        "spacing": "Small",
                    })
                    if loc or msg:
                        body.append({
                            "type": "TextBlock",
                            "text": f"📄 {loc} — {msg}" if loc else msg,
                            "size": "Small",
                            "isSubtle": True,
                            "wrap": True,
                            "spacing": "None",
                        })
                    if rule:
                        body.append({
                            "type": "TextBlock",
                            "text": f"Rule: {rule}",
                            "size": "Small",
                            "isSubtle": True,
                            "spacing": "None",
                        })
            else:
                body.append({
                    "type": "TextBlock",
                    "text": "✅ No critical issues today",
                    "size": "Small",
                    "color": "Good",
                    "spacing": "Small",
                })

            # medium / low counts
            body.append({
                "type": "TextBlock",
                "text": f"🟡 Medium: {med_cnt}  •  🟢 Low: {low_cnt}",
                "size": "Small",
                "spacing": "Small",
            })

            # commits / blocked / score
            blocked_dev = crit_cnt
            blocked_str = f"Blocked: {blocked_dev} ⚠️" if blocked_dev else "Blocked: 0"
            body.append({
                "type": "TextBlock",
                "text": f"📊 Commits: {commits}  •  {blocked_str}  •  Avg Score: {score10}/10",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Small",
            })

            body.append({
                "type": "TextBlock",
                "text": "─────────────────────────",
                "isSubtle": True,
                "size": "Small",
                "spacing": "Small",
            })

    # ── footer ────────────────────────────────────────────────────────────────
    body.append({
        "type": "TextBlock",
        "text": f"Generated {date_label}",
        "size": "Small",
        "isSubtle": True,
        "spacing": "Medium",
    })

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
        "actions": [{
            "type": "Action.OpenUrl",
            "title": "📊 Open Dashboard",
            "url": dashboard_url,
        }],
    }

    return {"target_email": tl_email, "card": card}


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


# ── kept for any callers that still reference the old names ──────────────────
def build_flat_payload(*args, **kwargs):
    return build_report_card(*args, **kwargs)

def build_project_wise_report(*args, **kwargs):
    return build_report_card(*args, **kwargs)

def build_adaptive_card(*args, **kwargs):
    return build_report_card(*args, **kwargs)

def send_team_report(webhook_url, tl_name, tl_email, summary, developer_stats,
                     date_label, dashboard_url="http://localhost:9090"):
    payload = build_report_card(
        projects_data=[],
        tl_name=tl_name or tl_email,
        tl_email=tl_email,
        date_label=date_label,
        dashboard_url=dashboard_url,
    )
    return post_to_teams(webhook_url, payload)
