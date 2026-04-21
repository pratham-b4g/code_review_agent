"""Microsoft Teams report delivery via Power Automate webhooks.

Each TL creates a Power Automate flow with the trigger
`When a Teams webhook request is received` and an action
`Post adaptive card in a chat or channel`. The flow returns a URL that we
POST an Adaptive Card JSON payload to.

Why Adaptive Cards (and not MessageCard / legacy connector cards)?
- Power Automate's "Post adaptive card" action takes an Adaptive Card 1.4+
  payload directly as JSON.
- Adaptive Cards render nicely on desktop + mobile Teams + Outlook.
- The legacy MessageCard ("Office 365 Connector Card") format is part of
  the retired connectors path and is discouraged for new integrations.

The webhook URL format is expected to be a Logic Apps / Power Automate
trigger URL (e.g. https://prod-XX.westus.logic.azure.com/workflows/.../triggers/manual/...).
We don't hardcode any tenant — each TL pastes their own URL.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:  # pragma: no cover - requests is a runtime dep of this package
    requests = None  # type: ignore[assignment]


# Dark-blue accent matching the dashboard theme
ACCENT_BLUE = "Accent"
WARNING = "Warning"
ATTENTION = "Attention"
GOOD = "Good"


def build_adaptive_card(summary: Dict[str, Any],
                        developer_stats: List[Dict[str, Any]],
                        tl_name: str,
                        tl_email: str,
                        date_label: str,
                        dashboard_url: str = "http://localhost:9090") -> Dict[str, Any]:
    """Return an Adaptive Card JSON wrapped in the Power Automate payload.

    Includes recipient info so Power Automate can route dynamically.
    Power Automate's "Post adaptive card in a chat" action expects:
      { "recipient": "user@email.com",
        "type": "message",
        "attachments": [ { "contentType": "application/vnd.microsoft.card.adaptive",
                           "content": <adaptive card> } ] }
    """
    total_commits   = int(summary.get("total_commits", 0) or 0)
    total_issues    = int(summary.get("total_issues", 0) or 0)
    avg_quality     = summary.get("avg_quality", 0)
    avg_effort      = int(summary.get("avg_effort", 0) or 0)
    errors          = int(summary.get("total_errors", 0) or 0)
    warnings        = int(summary.get("total_warnings", 0) or 0)
    infos           = int(summary.get("total_infos", 0) or 0)

    # Top 5 developers by commits
    top_devs = sorted(
        (developer_stats or []),
        key=lambda d: (int(d.get("commits", 0) or 0)),
        reverse=True,
    )[:5]

    # ── Build the developer table as a Container of rows ──
    def _cell(text: str, weight: str = "Default", color: str = "Default") -> Dict[str, Any]:
        return {
            "type": "TextBlock",
            "text": text,
            "wrap": True,
            "size": "Small",
            "weight": weight,
            "color": color,
        }

    header_row = {
        "type": "ColumnSet",
        "columns": [
            {"type": "Column", "width": 3, "items": [_cell("Developer", "Bolder")]},
            {"type": "Column", "width": 1, "items": [_cell("Commits", "Bolder")]},
            {"type": "Column", "width": 1, "items": [_cell("Issues", "Bolder")]},
            {"type": "Column", "width": 1, "items": [_cell("Quality", "Bolder")]},
            {"type": "Column", "width": 1, "items": [_cell("Effort", "Bolder")]},
        ],
        "separator": True,
    }
    dev_rows = [header_row]
    for dev in top_devs:
        commits = int(dev.get("commits", 0) or 0)
        issues = int(dev.get("issues", 0) or 0)
        quality = dev.get("quality_score", 0)
        effort = int(dev.get("effort_score", 0) or 0)
        q_color = GOOD if (isinstance(quality, (int, float)) and quality >= 80) \
            else (WARNING if (isinstance(quality, (int, float)) and quality >= 60) else ATTENTION)
        dev_rows.append({
            "type": "ColumnSet",
            "spacing": "Small",
            "columns": [
                {"type": "Column", "width": 3, "items": [_cell(dev.get("name") or dev.get("email", "?"))]},
                {"type": "Column", "width": 1, "items": [_cell(str(commits))]},
                {"type": "Column", "width": 1, "items": [_cell(str(issues), color=ATTENTION if issues else "Default")]},
                {"type": "Column", "width": 1, "items": [_cell(f"{quality}%", color=q_color)]},
                {"type": "Column", "width": 1, "items": [_cell(str(effort))]},
            ],
        })
    if not top_devs:
        dev_rows.append({"type": "TextBlock", "text": "_No developer activity in this window._",
                         "wrap": True, "size": "Small", "isSubtle": True})

    # ── Per-project open-issue breakdown ──
    proj_lines: List[Dict[str, Any]] = []
    for p in (summary.get("project_summary") or [])[:10]:
        branches = " · ".join(
            f"{b.get('branch')}:{int(b.get('issues', 0) or 0)}{'*' if b.get('is_current') else ''}"
            for b in (p.get("branches") or [])[:6]
        )
        proj_lines.append({
            "type": "TextBlock",
            "wrap": True,
            "size": "Small",
            "text": f"**{p.get('project_name')}** — current: `{p.get('current_branch', '?')}` — "
                    f"deduped open: **{int(p.get('deduped_total', 0) or 0)}** — {branches}",
        })

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": [
            {
                "type": "TextBlock",
                "text": f"📊 Code Review Agent — Daily Report",
                "weight": "Bolder",
                "size": "Large",
                "color": ACCENT_BLUE,
            },
            {
                "type": "TextBlock",
                "text": f"Hi {tl_name}, here's your team summary for **{date_label}**.",
                "wrap": True,
                "spacing": "Small",
                "isSubtle": True,
            },
            {
                "type": "FactSet",
                "spacing": "Medium",
                "facts": [
                    {"title": "Commits",          "value": str(total_commits)},
                    {"title": "Open Issues",      "value": f"{total_issues}  "
                                                             f"(🔴 {errors}  🟡 {warnings}  🔵 {infos})"},
                    {"title": "Avg Quality",      "value": f"{avg_quality}%"},
                    {"title": "Total Effort",     "value": str(avg_effort)},
                ],
            },
            {
                "type": "TextBlock",
                "text": "Top developers",
                "weight": "Bolder",
                "size": "Medium",
                "spacing": "Large",
            },
            {"type": "Container", "items": dev_rows},
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Open Dashboard",
                "url": dashboard_url,
            },
        ],
    }
    if proj_lines:
        card["body"].append({
            "type": "TextBlock",
            "text": "Projects",
            "weight": "Bolder",
            "size": "Medium",
            "spacing": "Large",
        })
        card["body"].extend(proj_lines)

    # Return both the Adaptive Card and recipient for dynamic routing in Power Automate
    return {
        "recipient": tl_email,
        "recipient_name": tl_name,
        "report_type": "code_review_analytics",
        "date": date_label,
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }


def post_to_teams(webhook_url: str, payload: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    """POST the Adaptive Card payload to a Power Automate webhook.

    Returns `{ ok, status, body }`. Never raises — callers log + move on.
    """
    if not webhook_url:
        return {"ok": False, "status": 0, "body": "empty webhook url"}
    if requests is None:
        return {"ok": False, "status": 0, "body": "requests library not installed"}
    try:
        r = requests.post(webhook_url, json=payload, timeout=timeout,
                          headers={"Content-Type": "application/json"})
        ok = 200 <= r.status_code < 300
        # Power Automate typically returns 202 Accepted with an empty body.
        body = (r.text or "")[:300]
        return {"ok": ok, "status": r.status_code, "body": body}
    except Exception as e:
        return {"ok": False, "status": 0, "body": f"{type(e).__name__}: {e}"}


def send_team_report(webhook_url: str, tl_name: str, tl_email: str,
                     summary: Dict[str, Any], developer_stats: List[Dict[str, Any]],
                     date_label: str,
                     dashboard_url: str = "http://localhost:9090") -> Dict[str, Any]:
    """Convenience: build + send. Returns the post result dict."""
    payload = build_adaptive_card(
        summary=summary,
        developer_stats=developer_stats,
        tl_name=tl_name or tl_email,
        tl_email=tl_email,
        date_label=date_label,
        dashboard_url=dashboard_url,
    )
    return post_to_teams(webhook_url, payload)
