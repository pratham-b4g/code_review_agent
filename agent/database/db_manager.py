"""PostgreSQL database manager for CRA multi-user system."""
import os
import threading
from contextlib import contextmanager
from datetime import datetime, date
from typing import Optional, List, Dict, Any
import psycopg2
from psycopg2 import pool as _pg_pool
from psycopg2.extras import RealDictCursor

# Single source of truth — shared Neon cloud Postgres + env overrides
from agent.config.auth_config import (
    DATABASE_URL as DEFAULT_DB_URL,
    SUPER_ADMIN_EMAIL,
    SUPER_ADMIN_PASSWORD,
)


# ── Process-wide connection pool ────────────────────────────────────────
# Opening a TLS connection to remote Postgres (e.g. Neon us-east-1) is
# expensive (~300-600ms). We keep a small pool of long-lived connections
# so HTTP handlers reuse them across requests instead of reconnecting.
_POOL: Optional[_pg_pool.ThreadedConnectionPool] = None
_POOL_LOCK = threading.Lock()
_POOL_URL: Optional[str] = None


def _get_pool(db_url: str) -> _pg_pool.ThreadedConnectionPool:
    global _POOL, _POOL_URL
    with _POOL_LOCK:
        if _POOL is None or _POOL_URL != db_url:
            if _POOL is not None:
                try:
                    _POOL.closeall()
                except Exception:
                    pass
            minconn = int(os.getenv("CRA_DB_POOL_MIN", "1"))
            maxconn = int(os.getenv("CRA_DB_POOL_MAX", "8"))
            # keepalives keep idle TCP sockets alive through NAT/timeouts,
            # so Neon's autosuspend-resume cycle doesn't kill our pool.
            _POOL = _pg_pool.ThreadedConnectionPool(
                minconn, maxconn, db_url,
                keepalives=1, keepalives_idle=30,
                keepalives_interval=10, keepalives_count=3,
                connect_timeout=10,
            )
            _POOL_URL = db_url
    return _POOL


class DatabaseManager:
    """Thin wrapper around a process-wide pooled Postgres connection.

    Usage (unchanged from previous API):
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(...)

    The outer `with` now transparently acquires a connection from the pool,
    commits (or rolls back on exception), and returns it to the pool.
    """

    def __init__(self, db_url: Optional[str] = None):
        # Priority: explicit arg > CRA_DATABASE_URL env > auth_config default (Neon)
        self.db_url = db_url or os.getenv("CRA_DATABASE_URL") or DEFAULT_DB_URL

    @contextmanager
    def connect(self):
        pool_ = _get_pool(self.db_url)
        conn = pool_.getconn()
        broken = False
        try:
            yield conn
            if not conn.closed:
                conn.commit()
        except psycopg2.OperationalError:
            # Socket dropped / server recycled — discard this conn entirely.
            broken = True
            raise
        except Exception:
            try:
                if conn and not conn.closed:
                    conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                pool_.putconn(conn, close=broken)
            except Exception:
                pass

    def init_schema(self):
        """Create all required tables."""
        with self.connect() as conn:
            with conn.cursor() as cur:
                # Users table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        email VARCHAR(255) UNIQUE NOT NULL,
                        name VARCHAR(255) NOT NULL,
                        role VARCHAR(50) NOT NULL CHECK (role IN ('super_admin', 'admin', 'developer')),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        created_by INTEGER REFERENCES users(id),
                        is_active BOOLEAN DEFAULT TRUE
                    )
                """)

                # Projects table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS projects (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(255) NOT NULL,
                        path TEXT NOT NULL,
                        main_branch VARCHAR(100) DEFAULT 'main',
                        created_by INTEGER REFERENCES users(id),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        is_active BOOLEAN DEFAULT TRUE,
                        project_key VARCHAR(64) UNIQUE,
                        repo_url VARCHAR(500)
                    )
                """)

                # Migrations for existing installs
                for _migration in [
                    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS project_key VARCHAR(64) UNIQUE",
                    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS repo_url VARCHAR(500)",
                    "ALTER TABLE project_scans ADD COLUMN IF NOT EXISTS violations_json JSONB DEFAULT '[]'::jsonb",
                    "ALTER TABLE project_scans ADD COLUMN IF NOT EXISTS ai_violations_json JSONB DEFAULT '[]'::jsonb",
                    "ALTER TABLE project_scans ADD COLUMN IF NOT EXISTS hook_violations_json JSONB DEFAULT '[]'::jsonb",
                ]:
                    try:
                        cur.execute(_migration)
                    except Exception:
                        pass

                # Project assignments (TLs and Developers assigned to projects)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS project_assignments (
                        id SERIAL PRIMARY KEY,
                        project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                        user_email VARCHAR(255) REFERENCES users(email),
                        assigned_by INTEGER REFERENCES users(id),
                        assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        role_on_project VARCHAR(50) CHECK (role_on_project IN ('tl', 'developer')),
                        UNIQUE(project_id, user_email)
                    )
                """)

                # Access requests (developers requesting access)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS access_requests (
                        id SERIAL PRIMARY KEY,
                        requester_email VARCHAR(255) NOT NULL,
                        requester_name VARCHAR(255) NOT NULL,
                        tl_email VARCHAR(255) NOT NULL,
                        project_id INTEGER REFERENCES projects(id),
                        status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
                        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        responded_at TIMESTAMP,
                        responded_by INTEGER REFERENCES users(id),
                        notes TEXT
                    )
                """)

                # Migration: Add project_id column if it doesn't exist
                try:
                    cur.execute("""
                        ALTER TABLE access_requests
                        ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id)
                    """)
                except Exception:
                    pass

                # Migration: per-TL Microsoft Teams report delivery settings.
                # Each TL configures their own Power Automate webhook URL,
                # preferred local time, and timezone. The scheduler fires
                # once per TL per day at their configured moment.
                try:
                    cur.execute("""
                        ALTER TABLE users
                            ADD COLUMN IF NOT EXISTS teams_webhook_url TEXT,
                            ADD COLUMN IF NOT EXISTS report_time VARCHAR(5),
                            ADD COLUMN IF NOT EXISTS report_timezone VARCHAR(64) DEFAULT 'Asia/Kolkata',
                            ADD COLUMN IF NOT EXISTS report_enabled BOOLEAN DEFAULT FALSE,
                            ADD COLUMN IF NOT EXISTS last_report_sent_on DATE
                    """)
                except Exception:
                    pass

                # Migration: report frequency and email delivery options
                try:
                    cur.execute("""
                        ALTER TABLE users
                            ADD COLUMN IF NOT EXISTS report_frequency VARCHAR(10) DEFAULT 'daily',
                            ADD COLUMN IF NOT EXISTS email_reports_enabled BOOLEAN DEFAULT FALSE
                    """)
                except Exception:
                    pass

                # Analytics data (commits, issues, etc.)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS developer_analytics (
                        id SERIAL PRIMARY KEY,
                        user_email VARCHAR(255) REFERENCES users(email),
                        project_id INTEGER REFERENCES projects(id),
                        branch VARCHAR(100) DEFAULT 'main',
                        date DATE NOT NULL,
                        commits_count INTEGER DEFAULT 0,
                        lines_added INTEGER DEFAULT 0,
                        lines_removed INTEGER DEFAULT 0,
                        issues_found INTEGER DEFAULT 0,
                        bugs_fixed INTEGER DEFAULT 0,
                        files_changed INTEGER DEFAULT 0,
                        code_quality_score DECIMAL(5,2),
                        effort_score DECIMAL(5,2),
                        blocked_commits INTEGER DEFAULT 0,
                        UNIQUE(user_email, project_id, branch, date)
                    )
                """)
                
                # Migration: Add branch column if it doesn't exist (for existing databases)
                try:
                    cur.execute("""
                        ALTER TABLE developer_analytics 
                        ADD COLUMN IF NOT EXISTS branch VARCHAR(100) DEFAULT 'main'
                    """)
                    # Update unique constraint
                    cur.execute("""
                        DO $$
                        BEGIN
                            IF EXISTS (
                                SELECT 1 FROM pg_indexes 
                                WHERE indexname = 'developer_analytics_user_email_project_id_date_key'
                            ) THEN
                                ALTER TABLE developer_analytics 
                                DROP CONSTRAINT developer_analytics_user_email_project_id_date_key;
                                
                                ALTER TABLE developer_analytics 
                                ADD CONSTRAINT developer_analytics_user_email_project_id_branch_date_key 
                                UNIQUE(user_email, project_id, branch, date);
                            END IF;
                        END $$;
                    """)
                except Exception:
                    pass  # Column might already exist or other migration issues

                # Migration: add blocked_commits column for existing databases
                try:
                    cur.execute("""
                        ALTER TABLE developer_analytics
                        ADD COLUMN IF NOT EXISTS blocked_commits INTEGER DEFAULT 0
                    """)
                except Exception:
                    pass

                # Project scan snapshots — one row per (project, branch); holds LATEST scan.
                # Used for correct team analytics (no double-counting across branches).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS project_scans (
                        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        branch VARCHAR(255) NOT NULL,
                        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        scanned_by_email VARCHAR(255),
                        total_issues INTEGER DEFAULT 0,
                        errors INTEGER DEFAULT 0,
                        warnings INTEGER DEFAULT 0,
                        infos INTEGER DEFAULT 0,
                        files_with_issues JSONB DEFAULT '{}'::jsonb,
                        quality_score DECIMAL(5,2),
                        violations_json JSONB DEFAULT '[]'::jsonb,
                        ai_violations_json JSONB DEFAULT '[]'::jsonb,
                        hook_violations_json JSONB DEFAULT '[]'::jsonb,
                        PRIMARY KEY (project_id, branch)
                    )
                """)

                # Per-push-attempt review records — one row per push (blocked or allowed).
                # Source of truth for scheduled TL reports. Never aggregated or overwritten.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS developer_reviews (
                        id                   SERIAL PRIMARY KEY,
                        developer_email      TEXT NOT NULL,
                        project_id           INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                        branch               TEXT DEFAULT 'main',
                        language             TEXT NOT NULL DEFAULT '',
                        framework            TEXT,
                        quality_score        DECIMAL(4,1),
                        high_issues          INTEGER DEFAULT 0,
                        medium_issues        INTEGER DEFAULT 0,
                        low_issues           INTEGER DEFAULT 0,
                        blocked              BOOLEAN DEFAULT FALSE,
                        files_reviewed       INTEGER DEFAULT 0,
                        security_issues      INTEGER DEFAULT 0,
                        quality_issues       INTEGER DEFAULT 0,
                        style_issues         INTEGER DEFAULT 0,
                        performance_issues   INTEGER DEFAULT 0,
                        critical_issues_json JSONB DEFAULT '[]'::jsonb,
                        created_at           TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_devreviews_email_proj_date
                    ON developer_reviews (developer_email, project_id, created_at)
                """)

                # Email notifications log
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS email_notifications (
                        id SERIAL PRIMARY KEY,
                        recipient_email VARCHAR(255) NOT NULL,
                        subject TEXT NOT NULL,
                        body TEXT NOT NULL,
                        notification_type VARCHAR(100),
                        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed'))
                    )
                """)

                conn.commit()

    def is_first_run(self) -> bool:
        """Check if database is empty (no users yet)."""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                return cur.fetchone()[0] == 0

    def create_super_admin(self, email: str, name: str, password: str) -> bool:
        """Create the first super admin."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (email, name, role, is_active)
                        VALUES (%s, %s, 'super_admin', TRUE)
                        ON CONFLICT (email) DO NOTHING
                        RETURNING id
                    """, (email, name))
                    result = cur.fetchone()
                    conn.commit()
                    return result is not None
        except Exception:
            return False

    def verify_super_admin(self, email: str, password: str) -> bool:
        """Verify super admin credentials."""
        # For now, check against static credentials
        # In production, you'd hash and store in DB
        return email == SUPER_ADMIN_EMAIL and password == SUPER_ADMIN_PASSWORD

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT * FROM users WHERE email = %s AND is_active = TRUE", (email,))
                    result = cur.fetchone()
                    return dict(result) if result else None
        except Exception as e:
            print(f"[DB Error] get_user_by_email: {e}")
            return None

    def create_user(self, email: str, name: str, role: str, created_by: Optional[int] = None) -> bool:
        """Create a new user (TL or Developer)."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    if created_by:
                        cur.execute("""
                            INSERT INTO users (email, name, role, created_by)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (email) DO NOTHING
                        """, (email, name, role, created_by))
                    else:
                        cur.execute("""
                            INSERT INTO users (email, name, role)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (email) DO NOTHING
                        """, (email, name, role))
                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] create_user: {e}")
            return False

    def get_all_users(self, role: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all users, optionally filtered by role."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if role:
                        cur.execute("SELECT * FROM users WHERE role = %s ORDER BY created_at DESC", (role,))
                    else:
                        cur.execute("SELECT * FROM users ORDER BY created_at DESC")
                    result = cur.fetchall()
                    return [dict(row) for row in result] if result else []
        except Exception as e:
            print(f"[DB Error] get_all_users: {e}")
            return []

    # ── Per-TL Teams report settings ─────────────────────────────
    def get_report_settings(self, email: str) -> Dict[str, Any]:
        """Return the TL's report delivery settings (never None)."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT teams_webhook_url, report_time, report_timezone,
                               report_enabled, last_report_sent_on,
                               report_frequency, email_reports_enabled
                        FROM users WHERE email = %s
                    """, (email,))
                    row = cur.fetchone()
                    if not row:
                        return {}
                    return {
                        "teams_webhook_url": row.get("teams_webhook_url") or "",
                        "report_time": row.get("report_time") or "",
                        "report_timezone": row.get("report_timezone") or "Asia/Kolkata",
                        "report_enabled": bool(row.get("report_enabled")),
                        "report_frequency": row.get("report_frequency") or "daily",
                        "email_reports_enabled": bool(row.get("email_reports_enabled")),
                        "last_report_sent_on": (row["last_report_sent_on"].isoformat()
                                                if row.get("last_report_sent_on") else None),
                    }
        except Exception as e:
            print(f"[DB Error] get_report_settings: {e}")
            return {}

    def update_report_settings(self, email: str, *,
                               teams_webhook_url: Optional[str] = None,
                               report_time: Optional[str] = None,
                               report_timezone: Optional[str] = None,
                               report_enabled: Optional[bool] = None,
                               report_frequency: Optional[str] = None,
                               email_reports_enabled: Optional[bool] = None) -> bool:
        """Partial update — only non-None fields are written."""
        sets = []
        params: List[Any] = []
        if teams_webhook_url is not None:
            sets.append("teams_webhook_url = %s"); params.append(teams_webhook_url or None)
        if report_time is not None:
            # Accept "HH:MM" or empty to clear
            sets.append("report_time = %s"); params.append(report_time or None)
        if report_timezone is not None:
            sets.append("report_timezone = %s"); params.append(report_timezone or "Asia/Kolkata")
        if report_enabled is not None:
            sets.append("report_enabled = %s"); params.append(bool(report_enabled))
        if report_frequency is not None:
            sets.append("report_frequency = %s"); params.append(report_frequency or "daily")
        if email_reports_enabled is not None:
            sets.append("email_reports_enabled = %s"); params.append(bool(email_reports_enabled))
        if not sets:
            return True
        # Reset last_report_sent_on so the newly configured time fires today
        sets.append("last_report_sent_on = NULL")
        params.append(email)
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE email = %s", params)
                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] update_report_settings: {e}")
            return False

    def mark_report_sent(self, email: str, sent_on) -> None:
        """Record the date (UTC or local — caller decides) the report was sent."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET last_report_sent_on = %s WHERE email = %s",
                                (sent_on, email))
                    conn.commit()
        except Exception as e:
            print(f"[DB Error] mark_report_sent: {e}")

    def get_tls_with_schedules(self) -> List[Dict[str, Any]]:
        """Return every admin who has report enabled + time + (webhook OR email enabled)."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT email, name, teams_webhook_url, report_time,
                               report_timezone, last_report_sent_on,
                               report_frequency, email_reports_enabled
                        FROM users
                        WHERE role IN ('admin', 'super_admin')
                          AND is_active = TRUE
                          AND report_enabled = TRUE
                          AND report_time IS NOT NULL
                          AND report_time <> ''
                          AND (
                              (teams_webhook_url IS NOT NULL AND teams_webhook_url <> '')
                              OR email_reports_enabled = TRUE
                          )
                    """)
                    return [dict(r) for r in (cur.fetchall() or [])]
        except Exception as e:
            print(f"[DB Error] get_tls_with_schedules: {e}")
            return []

    def create_project(self, name: str, path: str, main_branch: str, created_by: int, repo_url: Optional[str] = None) -> tuple:
        """Create a new project. Returns (project_id, project_key).

        If a project with the same path already exists (case-insensitive,
        normalized), returns its existing id and project_key instead of
        creating a duplicate.
        """
        import secrets as _secrets
        try:
            norm_path = (path or "").strip().rstrip("/").rstrip(".git").replace("\\", "/").lower()
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Check for existing project with same normalized path
                    cur.execute("SELECT id, path, project_key, repo_url FROM projects WHERE is_active = TRUE")
                    for row in cur.fetchall():
                        existing_norm = (row["path"] or "").strip().rstrip("/").rstrip(".git").replace("\\", "/").lower()
                        if existing_norm == norm_path:
                            existing_key = row["project_key"] or ""
                            updates = {}
                            if not existing_key:
                                existing_key = _secrets.token_hex(8)
                                updates["project_key"] = existing_key
                            # Back-fill repo_url if newly provided
                            if repo_url and not row.get("repo_url"):
                                updates["repo_url"] = repo_url
                            if updates:
                                set_clause = ", ".join(f"{k} = %s" for k in updates)
                                cur.execute(
                                    f"UPDATE projects SET {set_clause} WHERE id = %s",
                                    (*updates.values(), row["id"]),
                                )
                                conn.commit()
                            return (row["id"], existing_key)
                    # No duplicate — create new with a fresh project_key
                    new_key = _secrets.token_hex(8)
                    if created_by:
                        cur.execute("""
                            INSERT INTO projects (name, path, main_branch, created_by, project_key, repo_url)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            RETURNING id
                        """, (name, path, main_branch, created_by, new_key, repo_url or None))
                    else:
                        cur.execute("""
                            INSERT INTO projects (name, path, main_branch, project_key, repo_url)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING id
                        """, (name, path, main_branch, new_key, repo_url or None))
                    result = cur.fetchone()
                    conn.commit()
                    return (result["id"], new_key) if result else (None, new_key)
        except Exception as e:
            print(f"[DB Error] create_project: {e}")
            return (None, "")

    def get_project_by_key(self, project_key: str) -> Optional[Dict[str, Any]]:
        """Look up a project by its project_key (used by the git hook)."""
        if not project_key:
            return None
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM projects WHERE project_key = %s AND is_active = TRUE",
                        (project_key,),
                    )
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            print(f"[DB Error] get_project_by_key: {e}")
            return None

    def get_all_projects(self) -> List[Dict[str, Any]]:
        """Get all projects."""
        with self.connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM projects WHERE is_active = TRUE ORDER BY created_at DESC")
                return [dict(row) for row in cur.fetchall()]

    def update_project_main_branch(self, project_id: int, main_branch: str) -> bool:
        """Update the configured main/default branch for a project."""
        if not main_branch:
            return False
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE projects SET main_branch = %s WHERE id = %s",
                        (main_branch, project_id),
                    )
                    conn.commit()
                    return cur.rowcount > 0
        except Exception as e:
            print(f"[DB Error] update_project_main_branch: {e}")
            return False

    def update_project_repo_url(self, project_id: int, repo_url: str) -> bool:
        """Set or update the GitHub URL for a project."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE projects SET repo_url = %s WHERE id = %s",
                        (repo_url or None, project_id),
                    )
                    conn.commit()
                    return cur.rowcount > 0
        except Exception as e:
            print(f"[DB Error] update_project_repo_url: {e}")
            return False

    def get_user_projects(self, user_email: str) -> List[Dict[str, Any]]:
        """Get projects assigned to a user."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT p.*, pa.role_on_project
                        FROM projects p
                        JOIN project_assignments pa ON p.id = pa.project_id
                        WHERE pa.user_email = %s AND p.is_active = TRUE
                        ORDER BY p.created_at DESC
                    """, (user_email,))
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"[DB Error] get_user_projects: {e}")
            return []

    def get_project_assignments(self, project_id: int) -> List[Dict[str, Any]]:
        """Get all users assigned to a project."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT pa.*, u.name, u.email, u.role as user_role
                        FROM project_assignments pa
                        JOIN users u ON pa.user_email = u.email
                        WHERE pa.project_id = %s AND u.is_active = TRUE
                        ORDER BY u.role, u.name
                    """, (project_id,))
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"[DB Error] get_project_assignments: {e}")
            return []

    def assign_user_to_project(self, project_id: int, user_email: str, role: str, assigned_by: int) -> bool:
        """Assign a user to a project."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO project_assignments (project_id, user_email, role_on_project, assigned_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (project_id, user_email) DO UPDATE
                        SET role_on_project = EXCLUDED.role_on_project, assigned_at = CURRENT_TIMESTAMP
                    """, (project_id, user_email, role, assigned_by))
                    conn.commit()
                    return True
        except Exception:
            return False

    def remove_user_from_project(self, project_id: int, user_email: str) -> bool:
        """Remove a user from a project."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        DELETE FROM project_assignments
                        WHERE project_id = %s AND user_email = %s
                    """, (project_id, user_email))
                    conn.commit()
                    return cur.rowcount > 0
        except Exception as e:
            print(f"[DB Error] remove_user_from_project: {e}")
            return False

    def create_access_request(self, requester_email: str, requester_name: str,
                              tl_email: str, project_id: Optional[int] = None) -> bool:
        """Create an access request from developer, optionally for a specific project."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO access_requests (requester_email, requester_name, tl_email, project_id)
                        VALUES (%s, %s, %s, %s)
                    """, (requester_email, requester_name, tl_email, project_id))
                    conn.commit()
                    return True
        except Exception:
            return False

    def get_pending_access_requests(self, tl_email: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get pending access requests with optional project info."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if tl_email:
                        cur.execute("""
                            SELECT ar.*, p.name as project_name
                            FROM access_requests ar
                            LEFT JOIN projects p ON ar.project_id = p.id
                            WHERE ar.tl_email = %s AND ar.status = 'pending'
                            ORDER BY ar.requested_at DESC
                        """, (tl_email,))
                    else:
                        cur.execute("""
                            SELECT ar.*, p.name as project_name
                            FROM access_requests ar
                            LEFT JOIN projects p ON ar.project_id = p.id
                            WHERE ar.status = 'pending'
                            ORDER BY ar.requested_at DESC
                        """)
                    return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"[DB Error] get_pending_access_requests: {e}")
            return []

    def respond_to_access_request(self, request_id: int, status: str, responded_by: int,
                                  notes: str = "", approved_role: str = "developer") -> bool:
        """Approve or reject an access request.

        On 'approved', the requester is created as a user with `approved_role`
        ('developer' or 'admin') if not already in the system, and — if the
        request targeted a specific project — is automatically assigned to
        that project with the corresponding role ('tl' for admin, 'developer'
        otherwise).

        Args:
            approved_role: 'developer' or 'admin' (TL). Ignored if status != approved.
        """
        try:
            if approved_role not in ("developer", "admin"):
                approved_role = "developer"
            project_role = "tl" if approved_role == "admin" else "developer"

            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT * FROM access_requests WHERE id = %s", (request_id,))
                    req = cur.fetchone()
                    if not req:
                        return False

                    cur.execute("""
                        UPDATE access_requests
                        SET status = %s, responded_at = CURRENT_TIMESTAMP, responded_by = %s, notes = %s
                        WHERE id = %s
                    """, (status, responded_by, notes, request_id))

                    if status == "approved":
                        # 1. Ensure user exists with the designated role (idempotent)
                        cur.execute("""
                            INSERT INTO users (email, name, role, created_by, is_active)
                            VALUES (%s, %s, %s, %s, TRUE)
                            ON CONFLICT (email) DO UPDATE
                                SET is_active = TRUE,
                                    role = EXCLUDED.role
                            RETURNING id
                        """, (req['requester_email'], req['requester_name'], approved_role, responded_by))

                        # 2. If request targeted a specific project, assign
                        if req.get('project_id'):
                            cur.execute("""
                                INSERT INTO project_assignments
                                    (project_id, user_email, role_on_project, assigned_by)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (project_id, user_email) DO UPDATE
                                    SET role_on_project = EXCLUDED.role_on_project,
                                        assigned_at = CURRENT_TIMESTAMP
                            """, (req['project_id'], req['requester_email'], project_role, responded_by))

                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] respond_to_access_request: {e}")
            return False

    def save_project_scan(self, project_id: int, branch: str, scanned_by_email: str,
                          violations: List[Dict[str, Any]],
                          quality_score: Optional[float] = None) -> bool:
        """Upsert the latest scan snapshot for (project, branch).

        Stores per-file issue counts so analytics can attribute issues to
        the developers who actually modified each file.
        """
        try:
            import json as _json
            # Normalize AI severity values (high/medium/low) to rule-engine values
            # (error/warning/info) so counts and attribution are consistent.
            _sev_norm = {"high": "error", "medium": "warning", "low": "info"}

            def _norm_sev(sev: str) -> str:
                return _sev_norm.get(sev, sev) if sev not in ("error", "warning", "info") else sev

            errors = sum(1 for v in violations if _norm_sev(v.get("severity", "info")) == "error")
            warnings = sum(1 for v in violations if _norm_sev(v.get("severity", "info")) == "warning")
            infos = sum(1 for v in violations if _norm_sev(v.get("severity", "info")) == "info")

            # Group violations by normalised file path
            files_with_issues: Dict[str, Dict[str, int]] = {}
            for v in violations:
                fp = (v.get("file") or "").replace("\\", "/").lstrip("./")
                if not fp:
                    continue
                bucket = files_with_issues.setdefault(fp, {
                    "errors": 0, "warnings": 0, "infos": 0, "total": 0
                })
                sev = _norm_sev(v.get("severity", "info"))
                if sev == "error":
                    bucket["errors"] += 1
                elif sev == "warning":
                    bucket["warnings"] += 1
                else:
                    bucket["infos"] += 1
                bucket["total"] += 1

            # Store full violation details (cap at 200 to keep payload manageable)
            # source field is preserved so admin re-scans can merge AI violations back in
            violations_to_store = [
                {
                    "source": v.get("source", "rules"),
                    "severity": _norm_sev(v.get("severity", "info")),
                    "file": (v.get("file") or "").replace("\\", "/"),
                    "line": v.get("line", 0),
                    "rule_id": v.get("rule_id", ""),
                    "category": v.get("category", ""),
                    "message": v.get("message", ""),
                }
                for v in violations[:200]
            ]

            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO project_scans
                            (project_id, branch, scanned_at, scanned_by_email,
                             total_issues, errors, warnings, infos,
                             files_with_issues, quality_score, violations_json)
                        VALUES (%s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_id, branch) DO UPDATE SET
                            scanned_at = EXCLUDED.scanned_at,
                            scanned_by_email = EXCLUDED.scanned_by_email,
                            total_issues = EXCLUDED.total_issues,
                            errors = EXCLUDED.errors,
                            warnings = EXCLUDED.warnings,
                            infos = EXCLUDED.infos,
                            files_with_issues = EXCLUDED.files_with_issues,
                            quality_score = EXCLUDED.quality_score,
                            violations_json = EXCLUDED.violations_json
                    """, (project_id, branch, scanned_by_email,
                          len(violations), errors, warnings, infos,
                          _json.dumps(files_with_issues), quality_score,
                          _json.dumps(violations_to_store)))
                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] save_project_scan: {e}")
            return False

    def save_hook_violations(self, project_id: int, branch: str, violations: List[Dict[str, Any]]) -> bool:
        """Accumulate rule-engine violations from hook runs into hook_violations_json.

        These are violations found on STAGED (uncommitted) code. Admin rescans
        never touch this column, so TLs can always see what blocked a commit
        even after the developer fixes the code and the admin rescans.
        Deduped by (file, line, rule_id). Capped at 200.
        """
        try:
            import json as _json
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO project_scans (project_id, branch)
                        VALUES (%s, %s)
                        ON CONFLICT (project_id, branch) DO NOTHING
                    """, (project_id, branch))
                    cur.execute(
                        "SELECT hook_violations_json FROM project_scans WHERE project_id=%s AND branch=%s",
                        (project_id, branch)
                    )
                    row = cur.fetchone()
                    existing = []
                    if row and row[0]:
                        try:
                            existing = _json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
                        except Exception:
                            existing = []
                    seen = {(v.get("file", ""), v.get("line", 0), v.get("rule_id", "")) for v in existing}
                    for v in violations:
                        key = (v.get("file", ""), v.get("line", 0), v.get("rule_id", ""))
                        if key not in seen:
                            existing.append(v)
                            seen.add(key)
                    merged = existing[:200]
                    cur.execute(
                        "UPDATE project_scans SET hook_violations_json=%s WHERE project_id=%s AND branch=%s",
                        (_json.dumps(merged), project_id, branch)
                    )
                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] save_hook_violations: {e}")
            return False

    def save_ai_violations(self, project_id: int, branch: str, ai_violations: List[Dict[str, Any]]) -> bool:
        """Accumulate AI violations into ai_violations_json without touching rule violations.

        Merges with existing AI violations (dedup by file+message[:80]) so
        multiple subproject hook runs don't lose each other's results.
        Ensures the project_scans row exists first (upserts a blank row if needed).
        """
        try:
            import json as _json
            with self.connect() as conn:
                with conn.cursor() as cur:
                    # Ensure row exists
                    cur.execute("""
                        INSERT INTO project_scans (project_id, branch)
                        VALUES (%s, %s)
                        ON CONFLICT (project_id, branch) DO NOTHING
                    """, (project_id, branch))

                    # Load existing AI violations
                    cur.execute(
                        "SELECT ai_violations_json FROM project_scans WHERE project_id=%s AND branch=%s",
                        (project_id, branch)
                    )
                    row = cur.fetchone()
                    existing = []
                    if row and row[0]:
                        try:
                            existing = _json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
                        except Exception:
                            existing = []

                    # Dedup: (file, message[:80]) as key
                    seen = {(v.get("file", ""), v.get("message", "")[:80]) for v in existing}
                    for v in ai_violations:
                        key = (v.get("file", ""), v.get("message", "")[:80])
                        if key not in seen:
                            existing.append(v)
                            seen.add(key)

                    # Cap at 100 AI violations total
                    merged = existing[:100]

                    cur.execute(
                        "UPDATE project_scans SET ai_violations_json=%s WHERE project_id=%s AND branch=%s",
                        (_json.dumps(merged), project_id, branch)
                    )
                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] save_ai_violations: {e}")
            return False

    def get_project_scans(self, project_id: Optional[int] = None,
                          branch: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch latest scan snapshots. Optionally filter by project/branch."""
        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    q = "SELECT * FROM project_scans WHERE 1=1"
                    params: list = []
                    if project_id is not None:
                        q += " AND project_id = %s"
                        params.append(project_id)
                    if branch is not None:
                        q += " AND branch = %s"
                        params.append(branch)
                    q += " ORDER BY project_id, branch"
                    cur.execute(q, params)
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[DB Error] get_project_scans: {e}")
            return []

    def save_developer_review(
        self,
        developer_email: str,
        project_id: int,
        branch: str,
        language: str,
        framework: str,
        quality_score: float,
        high_issues: int,
        medium_issues: int,
        low_issues: int,
        blocked: bool,
        files_reviewed: int,
        security_issues: int,
        quality_issues: int,
        style_issues: int,
        performance_issues: int,
        critical_issues: list,
    ) -> bool:
        """Insert one row per push attempt into developer_reviews.
        Every push (blocked or allowed) gets its own row — never aggregated."""
        import json as _j
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO developer_reviews
                        (developer_email, project_id, branch, language, framework,
                         quality_score, high_issues, medium_issues, low_issues,
                         blocked, files_reviewed, security_issues, quality_issues,
                         style_issues, performance_issues, critical_issues_json, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    """, (
                        developer_email, project_id, branch, language or '', framework or '',
                        quality_score, high_issues, medium_issues, low_issues,
                        blocked, files_reviewed, security_issues, quality_issues,
                        style_issues, performance_issues,
                        _j.dumps(critical_issues) if critical_issues else '[]',
                    ))
                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] save_developer_review: {e}")
            return False

    def log_analytics(self, user_email: str, project_id: int, date: date, **metrics) -> bool:
        """Log or update daily analytics for a developer."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    # Extract metrics with defaults
                    branch = metrics.get('branch', 'main')
                    commits = metrics.get('commits_count', 0)
                    lines_added = metrics.get('lines_added', 0)
                    lines_removed = metrics.get('lines_removed', 0)
                    issues = metrics.get('issues_found', 0)
                    bugs_fixed = metrics.get('bugs_fixed', 0)
                    files_changed = metrics.get('files_changed', 0)
                    quality_score = metrics.get('code_quality_score', None)
                    effort_score = metrics.get('effort_score', None)
                    blocked = metrics.get('blocked_commits', 0)

                    # Insert or update (upsert) - now includes branch
                    # For scans (commits_count=0), replace the daily value instead of accumulating.
                    # blocked_commits always accumulates regardless of commits_count.
                    # code_quality_score uses COALESCE so a NULL incoming value never overwrites a real score.
                    cur.execute("""
                        INSERT INTO developer_analytics
                        (user_email, project_id, branch, date, commits_count, lines_added, lines_removed,
                         issues_found, bugs_fixed, files_changed, code_quality_score, effort_score, blocked_commits)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (user_email, project_id, branch, date)
                        DO UPDATE SET
                            commits_count = CASE WHEN EXCLUDED.commits_count = 0
                                THEN developer_analytics.commits_count
                                ELSE developer_analytics.commits_count + EXCLUDED.commits_count END,
                            lines_added = CASE WHEN EXCLUDED.commits_count = 0
                                THEN EXCLUDED.lines_added
                                ELSE developer_analytics.lines_added + EXCLUDED.lines_added END,
                            lines_removed = CASE WHEN EXCLUDED.commits_count = 0
                                THEN EXCLUDED.lines_removed
                                ELSE developer_analytics.lines_removed + EXCLUDED.lines_removed END,
                            issues_found = CASE WHEN EXCLUDED.commits_count = 0
                                THEN EXCLUDED.issues_found
                                ELSE developer_analytics.issues_found + EXCLUDED.issues_found END,
                            bugs_fixed = developer_analytics.bugs_fixed + EXCLUDED.bugs_fixed,
                            files_changed = CASE WHEN EXCLUDED.commits_count = 0
                                THEN EXCLUDED.files_changed
                                ELSE developer_analytics.files_changed + EXCLUDED.files_changed END,
                            code_quality_score = COALESCE(EXCLUDED.code_quality_score, developer_analytics.code_quality_score),
                            effort_score = EXCLUDED.effort_score,
                            blocked_commits = developer_analytics.blocked_commits + EXCLUDED.blocked_commits
                    """, (user_email, project_id, branch, date, commits, lines_added, lines_removed,
                          issues, bugs_fixed, files_changed, quality_score, effort_score, blocked))
                    conn.commit()
                    return True
        except Exception as e:
            print(f"[DB Error] log_analytics: {e}")
            return False

    def get_analytics(self, user_email: Optional[str] = None, project_id: Optional[int] = None,
                     start_date: Optional[date] = None, end_date: Optional[date] = None,
                     branch: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get analytics data with filters."""
        with self.connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    SELECT da.*, u.name as user_name, p.name as project_name
                    FROM developer_analytics da
                    JOIN users u ON da.user_email = u.email
                    JOIN projects p ON da.project_id = p.id
                    WHERE 1=1
                """
                params = []

                if user_email:
                    query += " AND da.user_email = %s"
                    params.append(user_email)
                if project_id:
                    query += " AND da.project_id = %s"
                    params.append(project_id)
                if branch:
                    query += " AND da.branch = %s"
                    params.append(branch)
                if start_date:
                    query += " AND da.date >= %s"
                    params.append(start_date)
                if end_date:
                    query += " AND da.date <= %s"
                    params.append(end_date)

                query += " ORDER BY da.date DESC"

                cur.execute(query, params)
                return [dict(row) for row in cur.fetchall()]

    def get_tl_report_data(self, tl_email: str, days: int = 1) -> List[Dict[str, Any]]:
        """Return per-project, per-developer report data for scheduled TL reports.

        Reads from developer_reviews (one row per push attempt) — same data model as
        the local SQLite store, so commit counts, blocked counts, category breakdowns,
        and trends are all accurate and identical in format to the local report.
        """
        import json as _j
        from datetime import timedelta, timezone as _tz

        now_utc    = datetime.utcnow().replace(tzinfo=_tz.utc)
        curr_start = now_utc - timedelta(days=days)
        prev_start = now_utc - timedelta(days=days * 2)
        prev_end   = curr_start

        def _grade(s):
            if s >= 9:   return "A (Excellent)"
            if s >= 8:   return "B (Good)"
            if s >= 7:   return "C (Average)"
            return "D (Needs Improvement)"

        try:
            with self.connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:

                    # 1. TL role
                    cur.execute("SELECT role FROM users WHERE email = %s", (tl_email,))
                    tl_row  = cur.fetchone()
                    tl_role = (tl_row['role'] if tl_row else '') or ''
                    print(f"[Report] get_tl_report_data: tl={tl_email} role={tl_role}")

                    # 2. Projects visible to this TL
                    if tl_role == 'super_admin':
                        cur.execute("SELECT id, name FROM projects WHERE is_active = TRUE ORDER BY name")
                    else:
                        cur.execute("""
                            SELECT p.id, p.name FROM projects p
                            JOIN project_assignments pa ON p.id = pa.project_id
                            WHERE pa.user_email = %s AND p.is_active = TRUE ORDER BY p.name
                        """, (tl_email,))
                    projects = [dict(r) for r in cur.fetchall()]
                    print(f"[Report] found {len(projects)} project(s): {[p['name'] for p in projects]}")

                    result = []
                    for proj in projects:
                        pid   = proj['id']
                        pname = proj['name']

                        # 3. Assigned developers (excluding the TL themselves)
                        cur.execute("""
                            SELECT pa.user_email, u.name
                            FROM project_assignments pa
                            JOIN users u ON pa.user_email = u.email
                            WHERE pa.project_id = %s AND u.is_active = TRUE AND pa.user_email != %s
                        """, (pid, tl_email))
                        assigned = [dict(r) for r in cur.fetchall()]
                        if not assigned:
                            continue

                        dev_emails = [d['user_email'] for d in assigned]

                        # 4. Current-period aggregates per developer from developer_reviews
                        cur.execute("""
                            SELECT developer_email,
                                   COUNT(*)::int                                AS total_commits,
                                   COUNT(*) FILTER (WHERE blocked)::int         AS blocked_commits,
                                   COALESCE(AVG(NULLIF(quality_score,0)), 0)    AS avg_score,
                                   COALESCE(SUM(high_issues),         0)::int   AS high_issues,
                                   COALESCE(SUM(medium_issues),       0)::int   AS medium_issues,
                                   COALESCE(SUM(low_issues),          0)::int   AS low_issues,
                                   COALESCE(SUM(security_issues),     0)::int   AS security_issues,
                                   COALESCE(SUM(quality_issues),      0)::int   AS quality_issues,
                                   COALESCE(SUM(style_issues),        0)::int   AS style_issues,
                                   COALESCE(SUM(performance_issues),  0)::int   AS performance_issues
                            FROM developer_reviews
                            WHERE project_id = %s AND developer_email = ANY(%s) AND created_at > %s
                            GROUP BY developer_email
                        """, (pid, dev_emails, curr_start))
                        curr = {r['developer_email']: dict(r) for r in cur.fetchall()}

                        # 5. Previous-period aggregates (for trends)
                        cur.execute("""
                            SELECT developer_email,
                                   COUNT(*)::int                                AS total_commits,
                                   COUNT(*) FILTER (WHERE blocked)::int         AS blocked_commits,
                                   COALESCE(AVG(NULLIF(quality_score,0)), 0)    AS avg_score,
                                   COALESCE(SUM(high_issues),   0)::int         AS high_issues,
                                   COALESCE(SUM(medium_issues), 0)::int         AS medium_issues,
                                   COALESCE(SUM(low_issues),    0)::int         AS low_issues
                            FROM developer_reviews
                            WHERE project_id = %s AND developer_email = ANY(%s)
                              AND created_at > %s AND created_at <= %s
                            GROUP BY developer_email
                        """, (pid, dev_emails, prev_start, prev_end))
                        prev = {r['developer_email']: dict(r) for r in cur.fetchall()}

                        # 6. Critical issues — combine all rows' JSON arrays per developer
                        cur.execute("""
                            SELECT developer_email, critical_issues_json
                            FROM developer_reviews
                            WHERE project_id = %s AND developer_email = ANY(%s) AND created_at > %s
                              AND critical_issues_json IS NOT NULL
                              AND jsonb_array_length(critical_issues_json) > 0
                        """, (pid, dev_emails, curr_start))
                        dev_critical: Dict[str, list] = {}
                        for row in cur.fetchall():
                            email = row['developer_email']
                            raw   = row['critical_issues_json']
                            if isinstance(raw, str):
                                try: raw = _j.loads(raw)
                                except Exception: raw = []
                            issues = raw if isinstance(raw, list) else []
                            dev_critical.setdefault(email, []).extend(issues)

                        # 7. Build developer list
                        developers = []
                        for dev in assigned:
                            email  = dev['user_email']
                            c      = curr.get(email, {})
                            p_     = prev.get(email, {})

                            total_commits      = int(c.get('total_commits',      0))
                            blocked_commits    = int(c.get('blocked_commits',    0))
                            avg_score          = round(float(c.get('avg_score',  0)), 1)
                            high_issues        = int(c.get('high_issues',        0))
                            medium_issues      = int(c.get('medium_issues',      0))
                            low_issues         = int(c.get('low_issues',         0))
                            security_issues    = int(c.get('security_issues',    0))
                            quality_issues     = int(c.get('quality_issues',     0))
                            style_issues       = int(c.get('style_issues',       0))
                            performance_issues = int(c.get('performance_issues', 0))
                            critical_issues    = dev_critical.get(email, [])[:20]

                            p_commits  = int(p_.get('total_commits',   0))
                            p_blocked  = int(p_.get('blocked_commits', 0))
                            p_score    = round(float(p_.get('avg_score', 0)), 1)
                            p_high     = int(p_.get('high_issues',     0))
                            p_medium   = int(p_.get('medium_issues',   0))
                            p_low      = int(p_.get('low_issues',      0))

                            developers.append({
                                "name":               dev.get('name') or email,
                                "email":              email,
                                "total_commits":      total_commits,
                                "blocked_commits":    blocked_commits,
                                "avg_score":          avg_score,
                                "quality_grade":      _grade(avg_score),
                                "high_issues":        high_issues,
                                "medium_issues":      medium_issues,
                                "low_issues":         low_issues,
                                "security_issues":    security_issues,
                                "quality_issues":     quality_issues,
                                "style_issues":       style_issues,
                                "performance_issues": performance_issues,
                                "critical_issues":    critical_issues,
                                "commits_delta":      total_commits   - p_commits,
                                "blocked_delta":      blocked_commits - p_blocked,
                                "score_delta":        round(avg_score - p_score, 1),
                                "high_delta":         high_issues     - p_high,
                                "medium_delta":       medium_issues   - p_medium,
                                "low_delta":          low_issues      - p_low,
                            })

                        if not developers:
                            continue

                        proj_scores = [d['avg_score'] for d in developers if d['avg_score']]
                        result.append({
                            "project_name":    pname,
                            "project_id":      pid,
                            "total_commits":   sum(d['total_commits']   for d in developers),
                            "blocked_commits": sum(d['blocked_commits'] for d in developers),
                            "total_issues":    sum(d['high_issues'] + d['medium_issues'] + d['low_issues']
                                                   for d in developers),
                            "quality_score":   round(sum(proj_scores) / len(proj_scores), 1)
                                               if proj_scores else 0.0,
                            "developers":      developers,
                        })

                    return result

        except Exception as e:
            print(f"[DB Error] get_tl_report_data: {e}")
            import traceback; traceback.print_exc()
            return []

    def queue_email_notification(self, recipient: str, subject: str, body: str, notification_type: str) -> bool:
        """Queue an email notification."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO email_notifications (recipient_email, subject, body, notification_type)
                        VALUES (%s, %s, %s, %s)
                    """, (recipient, subject, body, notification_type))
                    conn.commit()
                    return True
        except Exception:
            return False

    def get_pending_emails(self) -> List[Dict[str, Any]]:
        """Get all pending email notifications."""
        with self.connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM email_notifications
                    WHERE status = 'pending'
                    ORDER BY sent_at ASC
                """)
                return [dict(row) for row in cur.fetchall()]

    def mark_email_sent(self, email_id: int, status: str = 'sent') -> bool:
        """Mark email as sent or failed."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE email_notifications
                        SET status = %s, sent_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (status, email_id))
                    conn.commit()
                    return True
        except Exception:
            return False
