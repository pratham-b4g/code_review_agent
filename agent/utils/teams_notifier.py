"""Microsoft Teams report delivery via Power Automate webhooks.

Project-wise detailed reports with severity breakdown and expandable sections.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:
    requests = None  # type: ignore[assignment]

# Severity colors for Teams Adaptive Cards
SEVERITY_COLORS = {
    "critical": "Attention",  # Red
    "high": "Warning",        # Orange
    "medium": "Accent",       # Blue
    "low": "Good",           # Green
    "error": "Attention",
    "warning": "Warning",
    "info": "Accent",
}


def build_project_wise_report(
    projects_data: List[Dict[str, Any]],
    tl_name: str,
    tl_email: str,
    date_label: str,
    report_type: str = "daily",  # "daily" or "monthly"
    dashboard_url: str = "http://localhost:9090"
) -> Dict[str, Any]:
    """Build project-wise detailed report with severity breakdown.

    Each project section includes:
    - Project name (centered)
    - Branch list with issue counts
    - Severity breakdown (Critical, High, Medium, Low)
    - Developer assignments with branch details
    - Expandable issue lists
    """
    card_body = []

    # Header
    title = "📊 Code Review Agent — Monthly Report" if report_type == "monthly" else "📊 Code Review Agent — Daily Report"
    card_body.append({
        "type": "TextBlock",
        "text": title,
        "weight": "Bolder",
        "size": "Large",
        "color": "Accent",
        "horizontalAlignment": "Center"
    })
    card_body.append({
        "type": "TextBlock",
        "text": f"Hi {tl_name}, here's your {report_type} summary for **{date_label}**.",
        "wrap": True,
        "spacing": "Small",
        "isSubtle": True,
        "horizontalAlignment": "Center"
    })

    # Overall summary (for monthly reports)
    if report_type == "monthly":
        total_commits = sum(p.get("total_commits", 0) for p in projects_data)
        total_issues = sum(p.get("total_issues", 0) for p in projects_data)
        card_body.append({
            "type": "Container",
            "style": "emphasis",
            "items": [{
                "type": "FactSet",
                "facts": [
                    {"title": "Total Projects", "value": str(len(projects_data))},
                    {"title": "Monthly Commits", "value": str(total_commits)},
                    {"title": "Total Open Issues", "value": str(total_issues)},
                ]
            }],
            "spacing": "Medium"
        })

    # Per-project sections
    for project in projects_data:
        proj_name = project.get("project_name", "Unknown Project")
        proj_id = project.get("project_id", 0)

        # Project header with centered name
        card_body.append({
            "type": "Container",
            "style": "emphasis",
            "items": [{
                "type": "TextBlock",
                "text": f"🏢 {proj_name}",
                "weight": "Bolder",
                "size": "Medium",
                "horizontalAlignment": "Center",
                "color": "Accent"
            }],
            "spacing": "Large"
        })

        # Branches summary
        branches = project.get("branches", [])
        if branches:
            branch_texts = []
            for b in branches:
                name = b.get("branch", "unknown")
                issues = b.get("issues", 0)
                current = "✓" if b.get("is_current") else ""
                branch_texts.append(f"`{name}`:{issues}{current}")

            card_body.append({
                "type": "TextBlock",
                "text": "Branches: " + " · ".join(branch_texts[:6]),
                "wrap": True,
                "size": "Small",
                "isSubtle": True
            })

        # Severity breakdown - use project quality score (average of branches)
        develop_branch = next((b for b in branches if b.get("branch") in ["develop", "main", "master"]), None)
        if develop_branch:
            severity = develop_branch.get("severity_breakdown", {})
            critical = severity.get("critical", 0)
            high = severity.get("high", 0)
            medium = severity.get("medium", 0)
            low = severity.get("low", 0)
            # Use project-level quality score (calculated as average of all branches)
            quality = project.get("quality_score", develop_branch.get("quality_score", 0))

            card_body.append({
                "type": "FactSet",
                "facts": [
                    {"title": "Quality Score", "value": f"{quality}%"},
                    {"title": "🔴 Critical", "value": str(critical)},
                    {"title": "🟠 High", "value": str(high)},
                    {"title": "🔵 Medium", "value": str(medium)},
                    {"title": "🟢 Low", "value": str(low)},
                ],
                "spacing": "Small"
            })

        # Developers assigned to this project
        developers = project.get("developers", [])
        if developers:
            card_body.append({
                "type": "TextBlock",
                "text": "👥 Developers",
                "weight": "Bolder",
                "size": "Small",
                "spacing": "Medium"
            })

            for dev in developers:
                dev_name = dev.get("name", dev.get("email", "Unknown"))
                dev_branch = dev.get("branch", "develop")
                dev_commits = dev.get("commits", 0)
                dev_issues = dev.get("issues", 0)
                dev_quality = dev.get("quality_score", 0)
                productive_hours = dev.get("productive_hours", 0)
                extra_hours = dev.get("extra_hours", 0)

                # Developer card with expandable details
                dev_container = {
                    "type": "Container",
                    "style": "default",
                    "items": [
                        {
                            "type": "ColumnSet",
                            "columns": [
                                {
                                    "type": "Column",
                                    "width": "stretch",
                                    "items": [{
                                        "type": "TextBlock",
                                        "text": f"**{dev_name}** ({dev_branch})",
                                        "weight": "Bolder",
                                        "size": "Small"
                                    }]
                                },
                                {
                                    "type": "Column",
                                    "width": "auto",
                                    "items": [{
                                        "type": "TextBlock",
                                        "text": f"{dev_commits} commits",
                                        "size": "Small",
                                        "isSubtle": True
                                    }]
                                }
                            ]
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Issues", "value": str(dev_issues)},
                                {"title": "Quality", "value": f"{dev_quality}%"},
                                {"title": "⏱️ Productive Hours", "value": str(productive_hours)},
                                {"title": "⏰ Extra Hours", "value": str(extra_hours) if extra_hours > 0 else "None"},
                            ],
                            "spacing": "Small"
                        }
                    ],
                    "spacing": "Small"
                }

                # Add expandable issue details if there are critical/high issues
                critical_count = dev.get("critical_count", 0)
                high_count = dev.get("high_count", 0)
                medium_count = dev.get("medium_count", 0)
                low_count = dev.get("low_count", 0)
                total_severity_issues = critical_count + high_count + medium_count + low_count

                issue_details = dev.get("issue_details", [])

                if total_severity_issues > 0:
                    issue_items = []

                    # Header showing breakdown
                    issue_items.append({
                        "type": "TextBlock",
                        "text": f"**Issue Breakdown:** 🔴{critical_count} 🟠{high_count} 🔵{medium_count} 🟢{low_count}",
                        "size": "Small",
                        "weight": "Bolder",
                        "spacing": "Small"
                    })
                    issue_items.append({"type": "TextBlock", "text": "", "spacing": "Small"})

                    # Show actual issue details with explanations
                    for detail in issue_details[:10]:  # Show top 10 issues
                        severity = detail.get("severity", "info")
                        color = SEVERITY_COLORS.get(severity, "Default")
                        emoji = "🔴" if severity == "critical" else "🟠" if severity == "high" else "🔵" if severity == "medium" else "🟢"

                        issue_container = {
                            "type": "Container",
                            "style": "emphasis" if severity in ["critical", "high"] else "default",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": f"{emoji} **{detail.get('title', 'Issue')}**",
                                    "size": "Small",
                                    "weight": "Bolder",
                                    "color": color,
                                    "wrap": True
                                },
                                {
                                    "type": "TextBlock",
                                    "text": f"📁 File: `{detail.get('file', 'unknown')}`",
                                    "size": "Small",
                                    "isSubtle": True,
                                    "wrap": True
                                }
                            ],
                            "spacing": "Small"
                        }

                        # Add explanation if available
                        if detail.get("explanation"):
                            issue_container["items"].append({
                                "type": "TextBlock",
                                "text": f"� **Why:** {detail.get('explanation')}",
                                "size": "Small",
                                "wrap": True
                            })

                        # Add fix suggestion if available
                        if detail.get("fix"):
                            issue_container["items"].append({
                                "type": "TextBlock",
                                "text": f"🔧 **Fix:** {detail.get('fix')}",
                                "size": "Small",
                                "color": "Good",
                                "wrap": True
                            })

                        issue_items.append(issue_container)

                    # Show message if more issues exist
                    if len(issue_details) > 10:
                        issue_items.append({
                            "type": "TextBlock",
                            "text": f"... and {len(issue_details) - 10} more issues. View full details in dashboard.",
                            "size": "Small",
                            "isSubtle": True,
                            "italic": True
                        })

                    dev_container["items"].append({
                        "type": "ActionSet",
                        "actions": [{
                            "type": "Action.ShowCard",
                            "title": f"⚠️ View {total_severity_issues} Issues",
                            "card": {
                                "type": "AdaptiveCard",
                                "body": issue_items,
                                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                                "version": "1.4"
                            }
                        }],
                        "spacing": "Small"
                    })

                card_body.append(dev_container)

        # Separator between projects
        card_body.append({"type": "TextBlock", "text": "", "spacing": "Large"})

    # Footer actions
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": card_body,
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "📊 Open Dashboard",
                "url": dashboard_url,
            },
        ],
    }

    return {
        "recipient": tl_email,
        "recipient_name": tl_name,
        "date": date_label,
        "report_type": report_type,
        "card": card,
    }


# Legacy function - kept for backward compatibility
def build_adaptive_card(summary: Dict[str, Any],
                        developer_stats: List[Dict[str, Any]],
                        tl_name: str,
                        tl_email: str,
                        date_label: str,
                        dashboard_url: str = "http://localhost:9090") -> Dict[str, Any]:
    """Legacy simplified report - redirects to project-wise format."""
    # Convert old format to new project-wise format
    projects_data = []

    for p in summary.get("project_summary", []):
        project_devs = []
        for dev in developer_stats:
            if p.get("project_name") in str(dev.get("projects", [])):
                project_devs.append({
                    "name": dev.get("name", dev.get("email", "Unknown")),
                    "email": dev.get("email", ""),
                    "branch": dev.get("current_branch", "develop"),
                    "commits": dev.get("commits", 0),
                    "issues": dev.get("issues", 0),
                    "quality_score": dev.get("quality_score", 0),
                    "productive_hours": dev.get("productive_hours", 0),
                    "extra_hours": dev.get("extra_hours", 0),
                })

        projects_data.append({
            "project_name": p.get("project_name", "Unknown"),
            "project_id": p.get("project_id", 0),
            "branches": p.get("branches", []),
            "developers": project_devs,
            "total_commits": p.get("total_commits", 0),
            "total_issues": p.get("deduped_total", 0),
        })

    return build_project_wise_report(
        projects_data=projects_data,
        tl_name=tl_name,
        tl_email=tl_email,
        date_label=date_label,
        report_type="daily",
        dashboard_url=dashboard_url
    )


def post_to_teams(webhook_url: str, payload: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    """POST the payload to a Power Automate webhook.

    The payload should contain: { "recipient": "email", "card": {...}, ... }
    For Power Automate, we send the full payload including recipient for dynamic routing.
    For Teams webhook format, we wrap the card in the message envelope.

    Returns `{ ok, status, body }`. Never raises — callers log + move on.
    """
    if not webhook_url:
        return {"ok": False, "status": 0, "body": "empty webhook url"}
    if requests is None:
        return {"ok": False, "status": 0, "body": "requests library not installed"}

    # Send the full payload including recipient info
    # Power Automate will extract recipient from triggerBody()['recipient']
    # and the card from triggerBody()['card']
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
    # build_adaptive_card now returns { "recipient": ..., "card": ... }
    payload = build_adaptive_card(
        summary=summary,
        developer_stats=developer_stats,
        tl_name=tl_name or tl_email,
        tl_email=tl_email,
        date_label=date_label,
        dashboard_url=dashboard_url,
    )
    # Send the full payload including recipient for Power Automate dynamic routing
    return post_to_teams(webhook_url, payload)
