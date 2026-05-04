"""In-process scheduled report delivery.

Started once by the dashboard server. A single background thread wakes
every 30 s, checks each TL's `report_time` / `report_timezone` / `report_frequency`,
and fires their Teams webhook or email at the appropriate interval.

Design notes:
- Uses `zoneinfo` (Python 3.9+). Falls back to UTC if the configured tz
  can't be resolved.
- Dedup is DB-backed (`users.last_report_sent_on`) so restarts can't
  accidentally double-send.
- We tolerate a ±5 minute firing window (e.g. if the machine was asleep)
  so a configured 18:30 still fires at 18:33 after a wake.
- Supports daily, weekly (Monday), and monthly (1st of month) frequencies.
- Fires for admins and super_admins who have either Teams webhook OR email enabled.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - stdlib in py3.9+
    ZoneInfo = None  # type: ignore[assignment]


_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

# Firing tolerance: if we were asleep and wake up 3 min late, still fire.
_WINDOW_MINUTES = 5
_POLL_SECONDS = 30


def _now_in_tz(tz_name: str) -> datetime:
    """Return the current wall-clock datetime in the given IANA tz."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.utcnow()


def _parse_hhmm(value: str) -> Optional[tuple]:
    if not value:
        return None
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            return None
        hh, mm = int(parts[0]), int(parts[1])
        if 0 <= hh < 24 and 0 <= mm < 60:
            return (hh, mm)
    except Exception:
        pass
    return None


def _should_fire(now_local: datetime, hhmm: tuple, last_sent: Optional[date],
                 frequency: str = "daily") -> bool:
    """Check if report should fire based on time window and frequency."""
    hh, mm = hhmm
    target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (now_local - target).total_seconds() / 60.0
    # Within [0, WINDOW_MINUTES] minutes AFTER the target time
    if not (0 <= delta <= _WINDOW_MINUTES):
        return False
    
    today_local = now_local.date()
    
    # Check if already sent for this period
    if last_sent is None:
        return True
    
    freq = (frequency or "daily").lower()
    
    if freq == "daily":
        # Not already sent today
        return last_sent != today_local
    
    elif freq == "weekly":
        # Only fire on Mondays, and not already sent this week
        if now_local.weekday() != 0:  # Monday = 0
            return False
        # Check if sent in last 7 days (roughly same week)
        days_since_last = (today_local - last_sent).days
        return days_since_last >= 6
    
    elif freq == "monthly":
        # Only fire on 1st of month, and not already sent this month
        if today_local.day != 1:
            return False
        # Check if already sent this month
        return last_sent.month != today_local.month or last_sent.year != today_local.year
    
    return True


def _send_report_for(db, tracker, teams_mod, email_mod, tl: dict,
                     dashboard_url: str) -> bool:
    """Build analytics, send via Teams and/or email, mark success. Returns True if any sent."""
    email = tl["email"]
    tz_name = tl.get("report_timezone") or "Asia/Kolkata"
    now_local = _now_in_tz(tz_name)
    frequency = tl.get("report_frequency") or "daily"
    
    # Determine days window based on frequency
    if frequency == "weekly":
        days = 7
    elif frequency == "monthly":
        days = 30
    else:
        days = 7  # daily report shows last 7 days so there's always data
    
    # Get project-wise summary with severity breakdown and productivity metrics
    projects_data = tracker.get_project_wise_summary(
        tl_email=email, days=days, viewer_role="super_admin"
    )

    date_label = now_local.strftime("%A, %d %b %Y")
    any_success = False

    # Send to Teams if webhook configured
    webhook_url = tl.get("teams_webhook_url") or ""
    if webhook_url:
        try:
            report_payload = teams_mod.build_project_wise_report(
                projects_data=projects_data,
                tl_name=tl.get("name") or email,
                tl_email=email,
                date_label=date_label,
                report_type=frequency,
                dashboard_url=dashboard_url,
            )
            result = teams_mod.post_to_teams(webhook_url, report_payload)
            if result.get("ok"):
                print(f"[Scheduler] Teams report sent to {email} (HTTP {result.get('status')})")
                any_success = True
            else:
                print(f"[Scheduler] Teams report FAILED for {email}: HTTP {result.get('status')} — {result.get('body')}")
        except Exception as e:
            print(f"[Scheduler] Teams report error for {email}: {e}")
    
    # Send email if enabled
    email_enabled = tl.get("email_reports_enabled") or False
    if email_enabled:
        try:
            ok = email_mod.send_daily_analytics_report(
                tl_email=email,
                tl_name=tl.get("name") or email,
                date=date_label,
                summary=summary,
                developer_stats=summary.get("developers", []),
            )
            if ok:
                print(f"[Scheduler] Email report sent to {email}")
                any_success = True
            else:
                print(f"[Scheduler] Email report FAILED for {email}")
        except Exception as e:
            print(f"[Scheduler] Email report error for {email}: {e}")
    
    # Mark as sent if at least one channel succeeded
    if any_success:
        try:
            db.mark_report_sent(email, now_local.date())
        except Exception as e:
            print(f"[Scheduler] mark_report_sent failed for {email}: {e}")
        return True
    
    return False


def _tick(get_db, dashboard_url: str) -> None:
    """Single pass: check every enabled TL and fire if due based on frequency."""
    try:
        from agent.analytics import get_tracker
        from agent.utils import teams_notifier
        from agent.utils.email_notifier import get_notifier as get_email_notifier
    except Exception as e:
        print(f"[Scheduler] import error: {e}")
        return
    try:
        db = get_db()
        tracker = get_tracker()
        email_notifier = get_email_notifier()
    except Exception as e:
        print(f"[Scheduler] could not acquire DB/tracker: {e}")
        return

    try:
        tls = db.get_tls_with_schedules()
    except Exception as e:
        print(f"[Scheduler] could not list TLs: {e}")
        return

    for tl in tls:
        hhmm = _parse_hhmm(tl.get("report_time") or "")
        if not hhmm:
            continue
        tz_name = tl.get("report_timezone") or "Asia/Kolkata"
        frequency = tl.get("report_frequency") or "daily"
        now_local = _now_in_tz(tz_name)
        last_sent = tl.get("last_report_sent_on")
        if hasattr(last_sent, "isoformat"):
            # Postgres returns a datetime.date
            last_sent_date = last_sent if isinstance(last_sent, date) else last_sent.date()
        else:
            last_sent_date = None
        
        if _should_fire(now_local, hhmm, last_sent_date, frequency):
            print(f"[Scheduler] Firing {frequency} report for {tl.get('email')} at {now_local.strftime('%H:%M')}")
            try:
                _send_report_for(db, tracker, teams_notifier, email_notifier, tl, dashboard_url)
            except Exception as e:
                print(f"[Scheduler] unexpected error for {tl.get('email')}: {e}")


def _loop(get_db, dashboard_url: str) -> None:
    # Small jitter at startup so multiple dashboard restarts don't all
    # slam the webhook at the same second.
    _stop_event.wait(timeout=2.0)
    while not _stop_event.is_set():
        try:
            _tick(get_db, dashboard_url)
        except Exception as e:
            print(f"[Scheduler] tick error: {e}")
        _stop_event.wait(timeout=_POLL_SECONDS)


def start(get_db, dashboard_url: str = "http://localhost:9090") -> None:
    """Start the scheduler thread exactly once.

    `get_db` should be a callable returning a ready DatabaseManager (the
    dashboard already has `_get_db()` for this). We take a getter rather
    than a db instance so restarts / reconnects flow through the shared
    singleton.
    """
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    if os.getenv("CRA_DISABLE_REPORT_SCHEDULER") == "1":
        print("[Scheduler] disabled via CRA_DISABLE_REPORT_SCHEDULER")
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_loop, args=(get_db, dashboard_url),
        name="cra-report-scheduler", daemon=True,
    )
    _scheduler_thread.start()
    print("[Scheduler] Report scheduler started (polls every "
          f"{_POLL_SECONDS}s, fires within {_WINDOW_MINUTES}min window).")


def stop() -> None:
    _stop_event.set()
