"""Dynamic cron-style scheduled report delivery.

Each TL's report_time / report_timezone / report_frequency is stored in the DB.
The scheduler wakes at every minute boundary, reads the current config for all
TLs, and fires reports whose HH:MM matches right now in their timezone.

Key design choices
──────────────────
- Minute-aligned wakeups: sleeps precisely until the next :00 second so
  reports fire within ≤1 s of the configured time (was: ±5 min window).
- Dynamic config: settings are re-read from the DB on every tick, so a
  config change made in the admin panel takes effect within the next minute
  without any restart.
- Startup catch-up: if the server was down at the scheduled minute, it fires
  immediately if the configured time was within the last 15 minutes and the
  report hasn't been sent today.
- notify_settings_changed(): call this from the API handler after saving new
  settings to wake the scheduler early, giving sub-minute latency for manual
  triggers or immediate tests.
"""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover — stdlib in py3.9+
    ZoneInfo = None  # type: ignore[assignment]


_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_wake_event = threading.Event()   # pulsed by notify_settings_changed()

# On startup fire if the scheduled minute was missed within this window
_CATCHUP_MINUTES = 15


# ─── helpers ────────────────────────────────────────────────────────────────

def _now_in_tz(tz_name: str) -> datetime:
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


def _already_sent_today(last_sent, frequency: str, now_local: datetime) -> bool:
    """Return True if the report was already sent for the current period."""
    today = now_local.date()
    if last_sent is None:
        return False

    if isinstance(last_sent, datetime):
        last_sent = last_sent.date()

    freq = (frequency or "daily").lower()

    if freq == "daily":
        return last_sent >= today

    if freq == "weekly":
        # Monday-aligned weeks: sent this week means sent within last 6 days
        if now_local.weekday() != 0:   # Only fire on Monday
            return True                # Block firing on non-Mondays
        return (today - last_sent).days < 6

    if freq == "monthly":
        if today.day != 1:            # Only fire on the 1st
            return True
        return last_sent.month == today.month and last_sent.year == today.year

    return False


def _should_fire(now_local: datetime, hhmm: tuple, last_sent,
                 frequency: str, catchup: bool = False) -> bool:
    """Return True if a report should be sent right now.

    Normal mode: current hour:minute must match exactly.
    Catch-up mode: accept any time within the last CATCHUP_MINUTES minutes.
    """
    hh, mm = hhmm
    if catchup:
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (now_local - target).total_seconds() / 60.0
        if not (0 <= delta <= _CATCHUP_MINUTES):
            return False
    else:
        if now_local.hour != hh or now_local.minute != mm:
            return False

    return not _already_sent_today(last_sent, frequency, now_local)


# ─── send logic ─────────────────────────────────────────────────────────────

def _send_report_for(db, tracker, teams_mod, email_mod, tl: dict,
                     dashboard_url: str) -> bool:
    """Build analytics, send via Teams and/or email, mark success."""
    email = tl["email"]
    tz_name = tl.get("report_timezone") or "Asia/Kolkata"
    now_local = _now_in_tz(tz_name)
    frequency = tl.get("report_frequency") or "daily"

    days = 7 if frequency == "daily" else (7 if frequency == "weekly" else 30)

    projects_data = tracker.get_project_wise_summary(
        tl_email=email, days=days, viewer_role="super_admin"
    )

    date_label = now_local.strftime("%A, %d %b %Y")
    any_success = False

    webhook_url = tl.get("teams_webhook_url") or ""
    if webhook_url:
        try:
            payload = teams_mod.build_flat_payload(
                projects_data=projects_data,
                tl_name=tl.get("name") or email,
                tl_email=email,
                date_label=date_label,
                report_type=frequency,
            )
            result = teams_mod.post_to_teams(webhook_url, payload)
            if result.get("ok"):
                print(f"[Scheduler] Teams OK for {email} (HTTP {result.get('status')})")
                any_success = True
            else:
                print(f"[Scheduler] Teams FAILED for {email}: "
                      f"HTTP {result.get('status')} — {result.get('body')}")
        except Exception as exc:
            print(f"[Scheduler] Teams error for {email}: {exc}")

    if tl.get("email_reports_enabled"):
        try:
            ok = email_mod.send_daily_analytics_report(
                tl_email=email,
                tl_name=tl.get("name") or email,
                date=date_label,
                projects_data=projects_data,
            )
            if ok:
                print(f"[Scheduler] Email OK for {email}")
                any_success = True
            else:
                print(f"[Scheduler] Email FAILED for {email}")
        except Exception as exc:
            print(f"[Scheduler] Email error for {email}: {exc}")

    if any_success:
        try:
            db.mark_report_sent(email, now_local.date())
        except Exception as exc:
            print(f"[Scheduler] mark_report_sent failed for {email}: {exc}")
    return any_success


def _run_tick(get_db, dashboard_url: str, catchup: bool = False) -> None:
    """Single pass over all TLs — fire any whose time matches now."""
    try:
        from agent.analytics import get_tracker
        from agent.utils import teams_notifier
        from agent.utils.email_notifier import get_notifier as _get_email
    except Exception as exc:
        print(f"[Scheduler] import error: {exc}")
        return

    try:
        db = get_db()
        tracker = get_tracker()
        email_notifier = _get_email()
    except Exception as exc:
        print(f"[Scheduler] could not acquire DB/tracker: {exc}")
        return

    try:
        tls = db.get_tls_with_schedules()
    except Exception as exc:
        print(f"[Scheduler] could not list TLs: {exc}")
        return

    mode = "catch-up" if catchup else "tick"
    print(f"[Scheduler] {mode}: {len(tls)} TL(s) with active schedules")

    for tl in tls:
        email = tl.get("email", "?")
        hhmm = _parse_hhmm(tl.get("report_time") or "")
        if not hhmm:
            print(f"[Scheduler]   {email}: no valid report_time, skipping")
            continue

        tz_name = tl.get("report_timezone") or "Asia/Kolkata"
        frequency = tl.get("report_frequency") or "daily"
        now_local = _now_in_tz(tz_name)
        last_sent = tl.get("last_report_sent_on")
        configured = f"{hhmm[0]:02d}:{hhmm[1]:02d}"
        now_str = now_local.strftime("%H:%M")

        print(f"[Scheduler]   {email}: configured={configured} now={now_str} "
              f"tz={tz_name} freq={frequency} last_sent={last_sent}")

        if _should_fire(now_local, hhmm, last_sent, frequency, catchup=catchup):
            label = "catch-up" if catchup else frequency
            print(f"[Scheduler] >>> Firing {label} report for {email} at {now_str}")
            try:
                _send_report_for(db, tracker, teams_notifier, email_notifier,
                                 tl, dashboard_url)
            except Exception as exc:
                print(f"[Scheduler] unexpected error for {email}: {exc}")
        else:
            already = _already_sent_today(last_sent, frequency, now_local)
            print(f"[Scheduler]   {email}: skip — "
                  f"{'already sent today' if already else 'time does not match'}")


# ─── main loop ───────────────────────────────────────────────────────────────

def _seconds_to_next_minute() -> float:
    """Seconds until the top of the next minute (e.g. HH:MM+1:00)."""
    now = datetime.utcnow()
    return 60.0 - now.second - now.microsecond / 1_000_000


def _loop(get_db, dashboard_url: str) -> None:
    # Wait for DB to warm up (Neon cold-start can take ~5-10 s) then
    # run a catch-up pass in case the server restarted near a scheduled time.
    _stop_event.wait(timeout=12.0)
    if not _stop_event.is_set():
        print("[Scheduler] Running startup catch-up check...")
        try:
            _run_tick(get_db, dashboard_url, catchup=True)
        except Exception as exc:
            print(f"[Scheduler] startup catch-up error: {exc}")

    while not _stop_event.is_set():
        # Sleep until the next minute boundary.
        # _wake_event lets notify_settings_changed() interrupt the sleep early.
        sleep_secs = _seconds_to_next_minute()
        woken_early = _wake_event.wait(timeout=sleep_secs)
        if _stop_event.is_set():
            break
        if woken_early:
            _wake_event.clear()

        try:
            _run_tick(get_db, dashboard_url)
        except Exception as exc:
            print(f"[Scheduler] tick error: {exc}")


# ─── public API ──────────────────────────────────────────────────────────────

def notify_settings_changed() -> None:
    """Call after saving new report settings to apply them within seconds."""
    _wake_event.set()


def start(get_db, dashboard_url: str = "http://localhost:9090") -> None:
    """Start the scheduler thread exactly once."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    if os.getenv("CRA_DISABLE_REPORT_SCHEDULER") == "1":
        print("[Scheduler] disabled via CRA_DISABLE_REPORT_SCHEDULER=1")
        return
    _stop_event.clear()
    _wake_event.clear()
    _scheduler_thread = threading.Thread(
        target=_loop, args=(get_db, dashboard_url),
        name="cra-report-scheduler", daemon=True,
    )
    _scheduler_thread.start()
    print("[Scheduler] Dynamic cron scheduler started — fires at exact configured HH:MM.")


def stop() -> None:
    _stop_event.set()
    _wake_event.set()   # unblock any sleeping wait
