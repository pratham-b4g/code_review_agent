"""Email notification system for multi-user CRA using Power Automate."""
import os
import json
from typing import List, Optional, Dict, Any
from datetime import datetime

# Power Automate Flow URL — same pattern as local_store.py
_FLOW_URL = os.getenv(
    "CRA_FLOW_URL",
    "https://defaultcff20d814abd4f219998f39afd1df6.2a.environment.api.powerplatform.com:443"
    "/powerautomate/automations/direct/workflows/243a9a7a866c46dca7f63ba89b2feced"
    "/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun"
    "&sv=1.0&sig=YOxpQhyv1jIB2Cc2UDF7bX4PEXz0BTKb0Nnl2Kw7_RI",
)


class EmailNotifier:
    """Handles sending email notifications via Power Automate HTTP webhook."""

    def __init__(self, flow_url: Optional[str] = None):
        self.flow_url = flow_url or _FLOW_URL
        self.enabled = bool(self.flow_url)

    def _send_email(self, to_email: str, subject: str, body: str,
                   is_html: bool = True, extra_data: Optional[Dict] = None) -> bool:
        """Send an email via Power Automate HTTP POST."""
        import requests

        if not self.enabled:
            print(f"[Email] Would send to {to_email}: {subject}")
            print(f"[Email] Body preview: {body[:100]}...")
            return True

        try:
            payload = {
                "to_email": to_email,
                "subject": subject,
                "body": body,
                "is_html": is_html,
                "timestamp": datetime.now().isoformat(),
            }
            if extra_data:
                payload.update(extra_data)

            response = requests.post(
                self.flow_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"}
            )

            if response.status_code in (200, 202):
                return True
            else:
                print(f"[Email Error] Power Automate returned {response.status_code}: {response.text}")
                return False
        except Exception as e:
            print(f"[Email Error] Failed to send to {to_email}: {e}")
            return False

    def send_access_request_notification(self, tl_email: str, requester_name: str,
                                          requester_email: str) -> bool:
        """Notify TL that a developer requested access."""
        subject = f"[CRA] New Access Request from {requester_name}"

        body = f"""<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #58a6ff;">New Access Request</h2>
        <p><strong>{requester_name}</strong> ({requester_email}) has requested access to the Code Review Agent system.</p>
        <div style="background: #f6f8fa; padding: 15px; border-radius: 8px; margin: 20px 0;">
            <p><strong>Requester:</strong> {requester_name}</p>
            <p><strong>Email:</strong> {requester_email}</p>
            <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
        <p>Please log in to your CRA dashboard to approve or reject this request.</p>
        <a href="http://localhost:9090" style="display: inline-block; background: #58a6ff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin-top: 20px;">Open Dashboard</a>
    </div>
</body>
</html>"""

        return self._send_email(tl_email, subject, body, extra_data={
            "notification_type": "access_request",
            "requester_name": requester_name,
            "requester_email": requester_email
        })

    def send_access_request_response(self, requester_email: str, requester_name: str,
                                     status: str, tl_name: str, notes: str = "") -> bool:
        """Notify developer about access request approval/rejection."""
        is_approved = status == "approved"
        status_color = "#3fb950" if is_approved else "#f85149"
        status_text = "APPROVED" if is_approved else "REJECTED"

        subject = f"[CRA] Your Access Request has been {status_text}"

        next_steps = ("<p>You can now log in to the CRA dashboard using:</p>"
                     "<pre>cra dashboard</pre><p>Your email will be auto-detected from git config.</p>") if is_approved else ""
        notes_section = f"<div style='background: #fff8e1; padding: 15px; border-radius: 8px; margin: 20px 0;'><strong>Notes:</strong><br>{notes}</div>" if notes else ""

        body = f"""<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: {status_color};">Access Request {status_text}</h2>
        <p>Hello {requester_name},</p>
        <p>Your access request has been reviewed by <strong>{tl_name}</strong>.</p>
        <div style="background: {status_color}15; padding: 20px; border-radius: 8px; text-align: center;">
            <span style="font-size: 24px; font-weight: bold; color: {status_color};">{status_text}</span>
        </div>
        {notes_section}
        {next_steps}
    </div>
</body>
</html>"""

        return self._send_email(requester_email, subject, body, extra_data={
            "notification_type": "access_response",
            "status": status,
            "tl_name": tl_name
        })

    def send_project_assignment_notification(self, dev_email: str, dev_name: str,
                                             project_name: str, tl_name: str,
                                             role: str = "developer") -> bool:
        """Notify developer they've been assigned to a project."""
        subject = f"[CRA] You've been assigned to project: {project_name}"

        body = f"""<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #58a6ff;">New Project Assignment</h2>
        <p>Hello {dev_name},</p>
        <p>You have been assigned to a new project by <strong>{tl_name}</strong>.</p>
        <div style="background: #f6f8fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <p style="margin: 0;"><strong>Project:</strong> {project_name}</p>
            <p style="margin: 10px 0 0 0;"><strong>Your Role:</strong> {role.upper()}</p>
        </div>
        <p>To start working: <code>cra dashboard</code></p>
        <a href="http://localhost:9090" style="display: inline-block; background: #58a6ff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">Open Dashboard</a>
    </div>
</body>
</html>"""

        return self._send_email(dev_email, subject, body, extra_data={
            "notification_type": "project_assignment",
            "project_name": project_name,
            "role": role
        })

    def send_daily_analytics_report(self, tl_email: str, tl_name: str,
                                    date: str, summary: Dict[str, Any],
                                    developer_stats: List[Dict[str, Any]]) -> bool:
        """Send daily analytics report to TL via Power Automate."""
        # Build simple text version for Power Automate
        dev_lines = []
        for dev in developer_stats:
            dev_lines.append(f"  - {dev['name']}: {dev['commits']} commits, {dev['issues']} issues, {dev['quality_score']}% quality")

        body_text = f"""Daily Analytics Report - {date}

Team Summary:
- Total Commits: {summary.get('total_commits', 0)}
- Total Issues: {summary.get('total_issues', 0)}
- Average Quality: {summary.get('avg_quality', 0)}%
- Average Effort: {summary.get('avg_effort', 0)}

Developer Activity:
{chr(10).join(dev_lines) if dev_lines else "  No developer activity recorded."}

View full dashboard: http://localhost:9090
"""

        # Build HTML version
        dev_rows = ""
        for dev in developer_stats:
            dev_rows += f"<tr><td>{dev['name']}</td><td>{dev['commits']}</td><td>{dev['issues']}</td><td>{dev['quality_score']}%</td></tr>"

        body_html = f"""<html>
<body style="font-family: Arial, sans-serif;">
    <div style="max-width: 700px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #58a6ff;">Daily Analytics Report</h2>
        <p>{date}</p>
        <div style="background: #f6f8fa; padding: 20px; margin: 20px 0;">
            <p>Commits: <strong>{summary.get('total_commits', 0)}</strong></p>
            <p>Issues: <strong>{summary.get('total_issues', 0)}</strong></p>
            <p>Quality: <strong>{summary.get('avg_quality', 0)}%</strong></p>
        </div>
        <table style="width: 100%; border-collapse: collapse;">
            <tr style="background: #f6f8fa;"><th>Developer</th><th>Commits</th><th>Issues</th><th>Quality</th></tr>
            {dev_rows}
        </table>
    </div>
</body>
</html>"""

        return self._send_email(tl_email, f"[CRA] Daily Report - {date}", body_html,
                               extra_data={
                                   "notification_type": "daily_analytics",
                                   "summary": summary,
                                   "date": date,
                                   "text_body": body_text
                               })


# Global instance
_notifier: Optional[EmailNotifier] = None


def get_notifier(flow_url: Optional[str] = None) -> EmailNotifier:
    """Get or create the global email notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = EmailNotifier(flow_url)
    return _notifier


def configure_flow_url(url: str):
    """Configure Power Automate Flow URL programmatically."""
    global _notifier, _FLOW_URL
    _FLOW_URL = url
    _notifier = EmailNotifier(url)
