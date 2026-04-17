"""PostgreSQL database manager for CRA multi-user system."""
import os
from datetime import datetime, date
from typing import Optional, List, Dict, Any
import psycopg2
from psycopg2.extras import RealDictCursor

# Single source of truth — shared Neon cloud Postgres + env overrides
from agent.config.auth_config import (
    DATABASE_URL as DEFAULT_DB_URL,
    SUPER_ADMIN_EMAIL,
    SUPER_ADMIN_PASSWORD,
)


class DatabaseManager:
    """Manages PostgreSQL connection and all database operations."""

    def __init__(self, db_url: Optional[str] = None):
        # Priority: explicit arg > CRA_DATABASE_URL env > auth_config default (Neon)
        self.db_url = db_url or os.getenv("CRA_DATABASE_URL") or DEFAULT_DB_URL
        self.conn = None

    def connect(self):
        """Establish database connection."""
        if not self.conn or self.conn.closed:
            self.conn = psycopg2.connect(self.db_url)
        return self.conn

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
                        is_active BOOLEAN DEFAULT TRUE
                    )
                """)

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

    def create_user(self, email: str, name: str, role: str, created_by: int) -> bool:
        """Create a new user (TL or Developer)."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (email, name, role, created_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (email) DO NOTHING
                    """, (email, name, role, created_by))
                    conn.commit()
                    return True
        except Exception:
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

    def create_project(self, name: str, path: str, main_branch: str, created_by: int) -> Optional[int]:
        """Create a new project. If a project with the same path already exists
        (case-insensitive, normalized), return the existing project's id instead
        of creating a duplicate."""
        try:
            # Normalize the path for duplicate checking
            norm_path = (path or "").strip().rstrip("/").rstrip(".git").replace("\\", "/").lower()
            with self.connect() as conn:
                with conn.cursor() as cur:
                    # Check for existing project with same normalized path
                    cur.execute("SELECT id, path FROM projects WHERE is_active = TRUE")
                    for row in cur.fetchall():
                        existing_norm = (row[1] or "").strip().rstrip("/").rstrip(".git").replace("\\", "/").lower()
                        if existing_norm == norm_path:
                            return row[0]  # Return existing id, don't create duplicate
                    # No duplicate — create new
                    cur.execute("""
                        INSERT INTO projects (name, path, main_branch, created_by)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                    """, (name, path, main_branch, created_by))
                    result = cur.fetchone()
                    conn.commit()
                    return result[0] if result else None
        except Exception as e:
            print(f"[DB Error] create_project: {e}")
            return None

    def get_all_projects(self) -> List[Dict[str, Any]]:
        """Get all projects."""
        with self.connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM projects WHERE is_active = TRUE ORDER BY created_at DESC")
                return [dict(row) for row in cur.fetchall()]

    def get_user_projects(self, user_email: str) -> List[Dict[str, Any]]:
        """Get projects assigned to a user."""
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

                    # Insert or update (upsert) - now includes branch
                    # For scans (commits_count=0), replace the daily value instead of accumulating
                    cur.execute("""
                        INSERT INTO developer_analytics
                        (user_email, project_id, branch, date, commits_count, lines_added, lines_removed,
                         issues_found, bugs_fixed, files_changed, code_quality_score, effort_score)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                            code_quality_score = EXCLUDED.code_quality_score,
                            effort_score = EXCLUDED.effort_score
                    """, (user_email, project_id, branch, date, commits, lines_added, lines_removed,
                          issues, bugs_fixed, files_changed, quality_score, effort_score))
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
