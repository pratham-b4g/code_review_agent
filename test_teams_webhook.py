#!/usr/bin/env python3
"""Quick test for the Power Automate webhook URL."""
import json
import sys
from agent.utils.teams_notifier import build_adaptive_card, post_to_teams

# The webhook URL provided
WEBHOOK_URL = (
    "https://defaultcff20d814abd4f219998f39afd1df6.2a.environment.api.powerplatform.com:443"
    "/powerautomate/automations/direct/workflows/d8df3dc0a55641f5b9e0e59275370997"
    "/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0"
    "&sig=1JgJUHEy86m7X6x0iWGImIrfOtYKX0q9piYtFQ3sp_s"
)

# Sample test data
test_summary = {
    "total_commits": 42,
    "total_issues": 15,
    "avg_quality": 85,
    "avg_effort": 120,
    "total_errors": 3,
    "total_warnings": 8,
    "total_infos": 4,
    "project_summary": [
        {
            "project_name": "Test Project",
            "current_branch": "main",
            "deduped_total": 12,
            "branches": [{"branch": "main", "issues": 12, "is_current": True}]
        }
    ]
}

test_devs = [
    {
        "name": "Test Developer",
        "email": "dev@example.com",
        "commits": 15,
        "issues": 5,
        "quality_score": 88,
        "effort_score": 45
    },
    {
        "name": "Another Dev",
        "email": "another@example.com",
        "commits": 10,
        "issues": 3,
        "quality_score": 92,
        "effort_score": 38
    }
]

if __name__ == "__main__":
    print("Building Adaptive Card...")
    card = build_adaptive_card(
        summary=test_summary,
        developer_stats=test_devs,
        tl_name="Team Lead",
        tl_email="akash.kothari@biz4group.com",
        date_label="2026-04-21",
        dashboard_url="http://localhost:9090"
    )

    # Wrap for Teams webhook format
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card
            }
        ]
    }

    print("\nPayload preview (first 800 chars):")
    print(json.dumps(payload, indent=2)[:800] + "...")
    
    print("\nSending to Teams via Power Automate...")
    # post_to_teams now expects the raw card and wraps it internally
    result = post_to_teams(WEBHOOK_URL, card)

    print(f"\nResult: {result}")

    if result["ok"]:
        print("\n✅ SUCCESS! Check your Teams channel for the report.")
        sys.exit(0)
    else:
        print(f"\n❌ FAILED: {result['body']}")
        sys.exit(1)
