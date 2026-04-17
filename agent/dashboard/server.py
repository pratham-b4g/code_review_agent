"""Lightweight HTTP server that serves the CRA dashboard.

Supports multi-user mode with PostgreSQL backend for team collaboration.
"""

import json
import os
import sys
import threading
import webbrowser
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from agent.utils.logger import get_logger

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Multi-user mode flag and current user
_mode: str = "developer"  # 'admin' or 'developer'
_current_user: Optional[Dict[str, Any]] = None

# Launch context: set when dashboard is opened from a project directory
_launch_context: Dict[str, Any] = {}  # {project_dir, branch, repo_url}

# Global store for the latest scan results (populated by /api/scan or pre-loaded)
_scan_result: Dict[str, Any] = {}
_scan_lock = threading.Lock()


def _serialize_violations(violations: list) -> List[Dict[str, Any]]:
    """Convert Violation dataclass instances to plain dicts."""
    out = []
    for v in violations:
        out.append({
            "rule_id": v.rule_id,
            "rule_name": v.rule_name,
            "severity": v.severity.value if hasattr(v.severity, "value") else str(v.severity),
            "file_path": v.file_path,
            "line_number": v.line_number,
            "message": v.message,
            "fix_suggestion": v.fix_suggestion,
            "snippet": v.snippet,
            "category": v.category,
        })
    return out


def _get_db():
    """Get database manager instance."""
    from agent.database import DatabaseManager
    return DatabaseManager()


def _get_email_notifier():
    """Get email notifier instance."""
    from agent.utils.email_notifier import get_notifier
    return get_notifier()


def _run_scan(project_dir: str, language: Optional[str] = None,
              framework: Optional[str] = None) -> Dict[str, Any]:
    from agent.detector.language_detector import LanguageDetector
    from agent.detector.framework_detector import FrameworkDetector
    from agent.git.git_utils import scan_directory
    from agent.utils.config_manager import ConfigManager
    from agent.rules.rule_loader import RuleLoader
    from agent.rules.rule_engine import RuleEngine
    from agent.analyzer.cross_file_analyzer import (
        detect_cross_file_duplicates,
        detect_cross_file_constants,
        detect_missing_test_files,
        detect_architecture_issues,
    )
    from agent.utils.reporter import ReviewResult

    config = ConfigManager()
    lang = language or LanguageDetector(project_dir).detect_primary_language()
    fw = framework or FrameworkDetector(project_dir).detect()

    files = scan_directory(project_dir, lang, list(config.exclude_paths))
    if not files:
        return {"project": project_dir, "language": lang, "framework": fw,
                "files_scanned": 0, "violations": [], "duplication": {}}

    from agent.analyzer.python_analyzer import PythonAnalyzer
    from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer

    loader = RuleLoader()
    rules = loader.load_rules(language=lang, framework=fw)
    engine = RuleEngine(python_analyzer=PythonAnalyzer(), js_analyzer=JavaScriptAnalyzer())
    result = engine.review_files(files, rules, config.max_file_size_bytes, config.exclude_paths)
    result.deduplicate()

    # Cross-file analysis
    dup_violations, dup_stats = detect_cross_file_duplicates(files, lang)
    const_violations = detect_cross_file_constants(files, lang)
    test_violations = detect_missing_test_files(files, project_dir, lang)
    arch_violations = detect_architecture_issues(project_dir, lang, fw, files)
    result.violations.extend(dup_violations)
    result.violations.extend(const_violations)
    result.violations.extend(test_violations)
    result.violations.extend(arch_violations)
    result.deduplicate()

    # Build file source map (for inline code viewer)
    file_sources: Dict[str, List[str]] = {}
    for f in files:
        try:
            lines = Path(f).read_text(encoding="utf-8", errors="replace").splitlines()
            file_sources[f] = lines
        except OSError:
            pass

    return {
        "project": project_dir,
        "language": lang,
        "framework": fw or "",
        "files_scanned": result.files_scanned,
        "rules_applied": result.rules_applied,
        "violations": _serialize_violations(result.violations),
        "duplication": {
            "percentage": dup_stats.percentage,
            "duplicated_lines": dup_stats.duplicated_lines,
            "total_lines": dup_stats.total_lines,
        },
        "summary": {
            "errors": len(result.errors),
            "warnings": len(result.warnings),
            "infos": len(result.infos),
            "total": len(result.violations),
        },
        "files": list(file_sources.keys()),
        "file_sources": file_sources,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    """Handle API requests and serve static files."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_STATIC_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Multi-user mode: serve multi_user.html instead of index.html
        if _mode in ('admin', 'developer') and (path == "/" or path == ""):
            self.path = "/multi_user.html"
            return super().do_GET()

        # API: return scan data
        if path == "/api/data":
            with _scan_lock:
                data = dict(_scan_result)
            # Strip large file_sources from summary endpoint
            payload = {k: v for k, v in data.items() if k != "file_sources"}
            self._json_response(payload)
            return

        # API: return source for a specific file (legacy single-user mode)
        if path == "/api/file":
            qs = parse_qs(parsed.query)
            filepath = qs.get("path", [""])[0]
            # Normalise path separators for Windows compatibility
            normalised = os.path.normpath(filepath)
            with _scan_lock:
                sources = _scan_result.get("file_sources", {})
            lines = sources.get(filepath) or sources.get(normalised) or []
            # Fallback: read from disk if not in memory
            if not lines and os.path.isfile(normalised):
                try:
                    lines = Path(normalised).read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    pass
            self._json_response({"path": filepath, "lines": lines})
            return

        # API: return file content for multi-user project review
        if path == "/api/file-content":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            qs = parse_qs(parsed.query)
            filepath = qs.get("path", [""])[0]
            project_id = qs.get("project_id", [None])[0]
            if not filepath:
                self._json_response({"error": "path parameter required"}, 400)
                return
            try:
                normalised = os.path.normpath(filepath)
                
                # Try direct path first
                if os.path.isfile(normalised):
                    lines = Path(normalised).read_text(encoding="utf-8", errors="replace").splitlines()
                    self._json_response({"path": filepath, "lines": lines})
                    return
                
                # If not found and project_id provided, try to find in temp clone
                if project_id:
                    db = _get_db()
                    projects = db.get_all_projects()
                    project = next((p for p in projects if str(p["id"]) == project_id), None)
                    if project and project["path"].startswith(('http://', 'https://', 'git@')):
                        # Try to read from temp directory if scan was recent
                        import tempfile
                        import glob
                        temp_dirs = glob.glob(os.path.join(tempfile.gettempdir(), 'cra_scan_*'))
                        for temp_dir in sorted(temp_dirs, key=os.path.getmtime, reverse=True)[:3]:
                            temp_path = os.path.join(temp_dir, filepath)
                            if os.path.isfile(temp_path):
                                lines = Path(temp_path).read_text(encoding="utf-8", errors="replace").splitlines()
                                self._json_response({"path": filepath, "lines": lines})
                                return
                
                self._json_response({"error": "File not found"}, 404)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # API: trigger a new scan
        if path == "/api/scan":
            qs = parse_qs(parsed.query)
            project = qs.get("project", [""])[0]
            if not project:
                self._json_response({"error": "project parameter required"}, 400)
                return
            result = _run_scan(project)
            with _scan_lock:
                _scan_result.update(result)
            payload = {k: v for k, v in result.items() if k != "file_sources"}
            self._json_response(payload)
            return

        # ── Multi-User API Endpoints ────────────────────────────────────────

        # Check first run
        if path == "/api/auth/first-run":
            try:
                db = _get_db()
                db.init_schema()
                is_first = db.is_first_run()
                self._json_response({"is_first_run": is_first})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get current mode (admin or developer)
        if path == "/api/mode":
            self._json_response({"mode": _mode})
            return

        # Get launch context (project dir, branch, repo URL set at startup)
        if path == "/api/launch-context":
            self._json_response(_launch_context)
            return

        # Match a registered project by path or repo URL (authenticated)
        if path == "/api/match-project":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            qs = parse_qs(parsed.query)
            search_path = qs.get("path", [None])[0]
            search_url = qs.get("repo_url", [None])[0]
            if not search_path and not search_url:
                self._json_response({"error": "path or repo_url required"}, 400)
                return
            try:
                db = _get_db()
                projects = db.get_all_projects()
                matched = None

                # Normalize for comparison
                def _norm(s):
                    return (s or "").rstrip("/").rstrip(".git").replace("\\", "/").lower()

                for p in projects:
                    p_path = _norm(p["path"])
                    if search_url and _norm(search_url) == p_path:
                        matched = p
                        break
                    if search_path and _norm(search_path) == p_path:
                        matched = p
                        break
                    # Also check if the project path ends with the search dir name
                    if search_path:
                        dir_name = _norm(search_path).rstrip("/").split("/")[-1]
                        if dir_name and p_path.endswith("/" + dir_name):
                            matched = p
                            break

                if matched:
                    # Check if the user is assigned to this project
                    user_projects = db.get_user_projects(_current_user["email"])
                    is_assigned = any(up["id"] == matched["id"] for up in user_projects)

                    # Get TLs assigned to this project for access request
                    tls_on_project = []
                    if not is_assigned:
                        assignments = db.get_project_assignments(matched["id"])
                        tls_on_project = [
                            {"email": a["email"], "name": a["name"]}
                            for a in assignments if a.get("role_on_project") == "tl" or a.get("user_role") == "admin"
                        ]

                    self._json_response({
                        "found": True,
                        "project": matched,
                        "is_assigned": is_assigned,
                        "tls_on_project": tls_on_project
                    })
                else:
                    self._json_response({"found": False})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get git email from system
        if path == "/api/git-email":
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "config", "user.email"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                email = result.stdout.strip() if result.returncode == 0 else ""
                self._json_response({"email": email})
            except Exception:
                self._json_response({"email": ""})
            return

        # Get git user name from system
        if path == "/api/git-name":
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "config", "user.name"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                name = result.stdout.strip() if result.returncode == 0 else ""
                self._json_response({"name": name})
            except Exception:
                self._json_response({"name": ""})
            return

        # Get all TLs (for access request dropdown - allowed unauthenticated for request access flow)
        if path == "/api/users/tls":
            try:
                db = _get_db()
                tls = db.get_all_users(role='admin')
                # Only expose minimal info (name + email) for the dropdown
                self._json_response([{"email": u["email"], "name": u["name"]} for u in tls])
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get all users (super admin and TL only)
        if path == "/api/users":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: admin access required"}, 403)
                return
            try:
                db = _get_db()
                users = db.get_all_users()
                self._json_response(users)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get all projects (authenticated users only; super admin sees all)
        if path == "/api/projects":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            try:
                db = _get_db()
                projects = db.get_all_projects()
                self._json_response(projects)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get my projects (for TLs and developers)
        if path == "/api/my-projects":
            if not _current_user:
                self._json_response({"error": "Not authenticated"}, 401)
                return
            try:
                db = _get_db()
                projects = db.get_user_projects(_current_user["email"])
                self._json_response(projects)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get project assignments (super admin and TLs only)
        if path.startswith("/api/projects/") and path.endswith("/assignments"):
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: admin access required"}, 403)
                return
            try:
                project_id = int(path.split("/")[3])
                db = _get_db()
                assignments = db.get_project_assignments(project_id)
                self._json_response(assignments)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get user's project assignments (authenticated, super admin/admin or self only)
        if path == "/api/user-project-assignments":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            qs = parse_qs(parsed.query)
            user_email = qs.get("user_email", [None])[0]
            if not user_email:
                self._json_response({"error": "user_email required"}, 400)
                return
            # Developers can only query their own assignments
            if _current_user.get("role") == "developer" and user_email != _current_user.get("email"):
                self._json_response({"error": "Forbidden: can only view own assignments"}, 403)
                return
            try:
                db = _get_db()
                projects = db.get_user_projects(user_email)
                assignments = [{"project_id": p["id"], "user_email": user_email, "role_on_project": p.get("role_on_project", "developer")} for p in projects]
                self._json_response(assignments)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get project branches from Git
        if path.startswith("/api/projects/") and path.endswith("/branches"):
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            try:
                project_id = int(path.split("/")[3])
                db = _get_db()
                
                # Get project details
                projects = db.get_all_projects()
                project = next((p for p in projects if p["id"] == project_id), None)
                
                if not project:
                    self._json_response({"error": "Project not found"}, 404)
                    return
                
                # Get user role
                user_role = _current_user.get("role", "developer")
                user_email = _current_user["email"]
                
                # Fetch branches from Git
                import subprocess
                import tempfile
                import shutil
                
                project_path = project["path"]
                branches = []
                temp_dir = None
                
                try:
                    if project_path.startswith(('http://', 'https://', 'git@')):
                        # For remote repos, use git ls-remote to get branches
                        result = subprocess.run(
                            ['git', 'ls-remote', '--heads', project_path],
                            capture_output=True, text=True, timeout=30
                        )
                        if result.returncode == 0:
                            for line in result.stdout.strip().split('\n'):
                                if line:
                                    # Format: <sha>\trefs/heads/<branch>
                                    parts = line.split('\t')
                                    if len(parts) >= 2:
                                        branch = parts[1].replace('refs/heads/', '')
                                        branches.append(branch)
                    elif os.path.exists(project_path):
                        # For local repos
                        result = subprocess.run(
                            ['git', '-C', project_path, 'branch', '-r'],
                            capture_output=True, text=True, timeout=30
                        )
                        if result.returncode == 0:
                            for line in result.stdout.strip().split('\n'):
                                if line and '->' not in line:
                                    branch = line.strip().replace('origin/', '')
                                    if branch:
                                        branches.append(branch)
                        else:
                            # Try local branches
                            result = subprocess.run(
                                ['git', '-C', project_path, 'branch'],
                                capture_output=True, text=True, timeout=30
                            )
                            if result.returncode == 0:
                                for line in result.stdout.strip().split('\n'):
                                    if line:
                                        branch = line.strip().replace('* ', '')
                                        if branch:
                                            branches.append(branch)
                except Exception as e:
                    print(f"[Branches] Error fetching branches: {e}")
                    branches = ['main', 'develop']  # Fallback
                
                if not branches:
                    branches = ['main', 'develop']  # Default fallback
                
                # Remove duplicates and sort
                branches = sorted(list(set(branches)))
                
                # Filter branches based on user role
                if user_role == 'developer':
                    # For developers: include branches where they have (a) analytics entries,
                    # (b) actual git commits (authored), or (c) username-in-branch-name match.
                    developer_branches = set()

                    # (a) Branches with logged analytics
                    analytics = db.get_analytics(user_email=user_email, project_id=project_id)
                    for a in analytics:
                        if a.get('branch'):
                            developer_branches.add(a.get('branch'))

                    # (b) Branches with git commits authored by this developer
                    try:
                        from agent.analytics import get_tracker
                        tracker = get_tracker()
                        if os.path.exists(project_path):
                            for b in branches:
                                commits = tracker.get_commits_for_user_on_branch(
                                    project_path, b, user_email, since_days=365
                                )
                                if commits:
                                    developer_branches.add(b)
                    except Exception as e:
                        print(f"[Branches] git commit scan error: {e}")

                    # (c) Username in branch name
                    uname = user_email.split('@')[0].lower()
                    for b in branches:
                        if uname and uname in b.lower():
                            developer_branches.add(b)

                    all_branches_for_dev = branches  # keep full list for fallback
                    branches = [b for b in all_branches_for_dev if b in developer_branches]
                    if not branches:
                        # No authored commits yet — let dev work on any branch rather than
                        # locking them to main alone.
                        branches = all_branches_for_dev or ['main']
                
                # TL and super_admin see all branches
                self._json_response({
                    "branches": branches,
                    "role": user_role,
                    "filtered": user_role == 'developer'
                })
                
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Scan project for code review (with optional branch parameter)
        if path.startswith("/api/scan-project/"):
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            try:
                project_id = int(path.split("/")[3])
                db = _get_db()

                # Get project details
                projects = db.get_all_projects()
                project = next((p for p in projects if p["id"] == project_id), None)

                if not project:
                    self._json_response({"error": "Project not found"}, 404)
                    return

                # Super admin has unrestricted access to any project.
                # TLs and developers must be assigned to scan.
                if _current_user.get("role") != "super_admin":
                    user_projects = db.get_user_projects(_current_user["email"])
                    if not any(up["id"] == project_id for up in user_projects):
                        self._json_response({"error": "Not assigned to this project"}, 403)
                        return

                project_path = project["path"]

                # Check if branch parameter is specified
                qs = parse_qs(parsed.query)
                branch = qs.get("branch", [None])[0]

                if branch:
                    result = self._scan_project_branch(project_path, project_id, _current_user["email"], branch)
                else:
                    result = self._scan_project(project_path, project_id, _current_user["email"])

                self._json_response(result)

            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get access requests (authenticated: super admin sees all, TL sees own)
        if path == "/api/access-requests":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: admin access required"}, 403)
                return
            qs = parse_qs(parsed.query)
            tl_email = qs.get("tl_email", [None])[0]
            # TLs can only see requests addressed to them
            if _current_user.get("role") == "admin":
                tl_email = _current_user.get("email")
            try:
                db = _get_db()
                requests = db.get_pending_access_requests(tl_email)
                self._json_response(requests)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get analytics data (authenticated users only)
        if path == "/api/analytics":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            qs = parse_qs(parsed.query)
            user_email = qs.get("user_email", [None])[0]
            project_id = qs.get("project_id", [None])[0]
            days = int(qs.get("days", ["7"])[0])
            branch = qs.get("branch", [None])[0]  # None means all branches
            # Developers can only view their own analytics
            if _current_user.get("role") == "developer":
                user_email = _current_user.get("email")

            # If project_id is provided, convert to int
            if project_id:
                try:
                    project_id = int(project_id)
                except ValueError:
                    project_id = None

            try:
                from agent.analytics import get_tracker
                tracker = get_tracker()
                summary = tracker.get_analytics_summary(
                    project_id=project_id,
                    user_email=user_email,
                    days=days,
                    branch=branch
                )
                self._json_response(summary)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get detailed analytics (time series data) - authenticated only
        if path == "/api/analytics/detail":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            qs = parse_qs(parsed.query)
            user_email = qs.get("user_email", [None])[0]
            project_id = qs.get("project_id", [None])[0]
            days = int(qs.get("days", ["30"])[0])
            # Developers can only view their own analytics
            if _current_user.get("role") == "developer":
                user_email = _current_user.get("email")

            if project_id:
                try:
                    project_id = int(project_id)
                except ValueError:
                    project_id = None

            try:
                from datetime import date, timedelta
                db = _get_db()
                end_date = date.today()
                start_date = end_date - timedelta(days=days)

                data = db.get_analytics(
                    user_email=user_email,
                    project_id=project_id,
                    start_date=start_date,
                    end_date=end_date
                )
                self._json_response(data)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Static files
        if path == "/" or path == "":
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self):
        """Handle POST requests for multi-user operations."""
        global _current_user
        parsed = urlparse(self.path)
        path = parsed.path

        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode('utf-8')) if body else {}
        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, 400)
            return

        # First run setup
        if path == "/api/auth/first-run-setup":
            try:
                db = _get_db()
                success = db.create_super_admin(data.get("email"), data.get("name"), data.get("password"))
                if success:
                    _current_user = {"email": data["email"], "name": data["name"], "role": "super_admin", "id": 1}
                    self._json_response({"success": True, "user": _current_user})
                else:
                    self._json_response({"error": "Failed to create super admin"}, 500)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Login
        if path == "/api/auth/login":
            try:
                db = _get_db()
                mode = data.get("mode")

                if mode == "admin":
                    # Super admin login with password
                    from agent.config.auth_config import SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD
                    if data.get("email") == SUPER_ADMIN_EMAIL and data.get("password") == SUPER_ADMIN_PASSWORD:
                        # Get or create super admin in DB
                        try:
                            user = db.get_user_by_email(SUPER_ADMIN_EMAIL)
                            if not user:
                                create_success = db.create_user(SUPER_ADMIN_EMAIL, "Super Admin", "super_admin", None)
                                if create_success:
                                    user = db.get_user_by_email(SUPER_ADMIN_EMAIL)
                            if user:
                                _current_user = user
                                self._json_response({"success": True, "user": user})
                            else:
                                self._json_response({"error": "Failed to create/get super admin user"}, 500)
                        except Exception as db_error:
                            print(f"[Login Error] DB error: {db_error}")
                            self._json_response({"error": f"Database error: {str(db_error)}"}, 500)
                    else:
                        self._json_response({"error": "Invalid credentials"}, 401)

                else:
                    # Developer/TL login with email only
                    try:
                        user = db.get_user_by_email(data.get("email"))
                        if user:
                            _current_user = user
                            self._json_response({"success": True, "user": user})
                        else:
                            # Check if there are any TLs they can request from
                            tls = db.get_all_users(role='admin')
                            self._json_response({"success": False, "not_registered": True, "available_tls": len(tls) if tls else 0})
                    except Exception as db_error:
                        print(f"[Login Error] DB error: {db_error}")
                        self._json_response({"error": f"Database error: {str(db_error)}"}, 500)
            except Exception as e:
                print(f"[Login Error] General error: {e}")
                self._json_response({"error": str(e)}, 500)
            return

        # Create access request (optionally for a specific project)
        if path == "/api/access-requests":
            try:
                db = _get_db()
                # project_id is optional — if provided, the request is for a specific project
                project_id = data.get("project_id")
                if project_id is not None:
                    try:
                        project_id = int(project_id)
                    except (ValueError, TypeError):
                        project_id = None
                success = db.create_access_request(
                    data.get("requester_email"),
                    data.get("requester_name"),
                    data.get("tl_email"),
                    project_id
                )

                # Send email notification to TL
                if success:
                    notifier = _get_email_notifier()
                    notifier.send_access_request_notification(
                        data.get("tl_email"),
                        data.get("requester_name"),
                        data.get("requester_email")
                    )

                self._json_response({"success": success})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Respond to access request (authenticated super_admin or the TL the request is addressed to)
        if path.startswith("/api/access-requests/") and path.endswith("/respond"):
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: admin access required"}, 403)
                return
            try:
                request_id = int(path.split("/")[3])
                db = _get_db()

                # Get request details before responding
                requests = db.get_pending_access_requests()
                request_details = None
                for r in requests:
                    if r['id'] == request_id:
                        request_details = r
                        break

                # TLs can only respond to requests addressed to them
                if request_details and _current_user.get("role") == "admin":
                    if request_details.get("tl_email") != _current_user.get("email"):
                        self._json_response({"error": "Forbidden: this request is not addressed to you"}, 403)
                        return

                # Use server-side user id, NOT the client-supplied responded_by
                responded_by = _current_user.get("id")

                # Validate status value
                req_status = data.get("status")
                if req_status not in ("approved", "rejected"):
                    self._json_response({"error": "Invalid status. Must be 'approved' or 'rejected'"}, 400)
                    return

                # Role to grant on approval. Only super_admin may elevate to 'admin' (TL).
                approved_role = (data.get("approved_role") or "developer").lower()
                if approved_role not in ("developer", "admin"):
                    approved_role = "developer"
                if approved_role == "admin" and _current_user.get("role") != "super_admin":
                    self._json_response({"error": "Only super admin can approve as TL"}, 403)
                    return

                success = db.respond_to_access_request(
                    request_id,
                    req_status,
                    responded_by,
                    notes=data.get("notes", ""),
                    approved_role=approved_role
                )

                # Send email notification to developer
                if success and request_details:
                    notifier = _get_email_notifier()
                    # Get TL name
                    tl_user = db.get_user_by_email(request_details['tl_email'])
                    tl_name = tl_user['name'] if tl_user else request_details['tl_email']

                    notifier.send_access_request_response(
                        request_details['requester_email'],
                        request_details['requester_name'],
                        data.get("status"),
                        tl_name,
                        data.get("notes", "")
                    )

                self._json_response({"success": success})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Create user (super admin or TL). TLs can only create developers.
        if path == "/api/users":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: admins only"}, 403)
                return
            try:
                requested_role = data.get("role", "developer")
                # TLs can only create developers, not other TLs or super admins
                if _current_user.get("role") == "admin" and requested_role != "developer":
                    self._json_response({"error": "TLs can only create developers"}, 403)
                    return
                db = _get_db()
                # create_user uses ON CONFLICT (email) DO NOTHING — returns True even if already exists
                success = db.create_user(
                    data.get("email"),
                    data.get("name"),
                    requested_role,
                    _current_user.get("id")
                )
                # Look up user to check if they already existed (for UI feedback)
                existing = db.get_user_by_email(data.get("email"))
                already_existed = existing is not None
                self._json_response({
                    "success": success,
                    "already_existed": already_existed,
                    "user": existing
                })
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Create project (super admin or TL only)
        if path == "/api/projects":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: only admins can create projects"}, 403)
                return
            try:
                db = _get_db()
                # Check if a project with same path already exists
                norm_path = (data.get("path") or "").strip().rstrip("/").rstrip(".git").replace("\\", "/").lower()
                existing_projects = db.get_all_projects()
                pre_existing = None
                for p in existing_projects:
                    existing_norm = (p.get("path") or "").strip().rstrip("/").rstrip(".git").replace("\\", "/").lower()
                    if existing_norm == norm_path:
                        pre_existing = p
                        break
                project_id = db.create_project(
                    data.get("name"),
                    data.get("path"),
                    data.get("main_branch", "main"),
                    _current_user.get("id")
                )
                self._json_response({
                    "success": project_id is not None,
                    "project_id": project_id,
                    "already_existed": pre_existing is not None,
                    "existing_project": pre_existing
                })
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Backfill git history for a project (TL/super_admin) — optionally filter to one user_email
        if path.startswith("/api/projects/") and path.endswith("/backfill"):
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden"}, 403)
                return
            try:
                parts = path.split("/")
                project_id = int(parts[3])
                db = _get_db()
                projects = db.get_all_projects()
                project = next((p for p in projects if p['id'] == project_id), None)
                if not project:
                    self._json_response({"error": "Project not found"}, 404)
                    return
                project_path = project.get('path')
                if not project_path or not os.path.exists(project_path):
                    self._json_response({
                        "error": f"Project path not accessible: {project_path}. "
                                 "Clone the repo locally (or have each dev run `cra scan` inside it) then retry."
                    }, 400)
                    return

                from agent.analytics import get_tracker
                tracker = get_tracker()
                # Determine which users to backfill
                target_email = data.get("user_email")
                if target_email:
                    targets = [target_email]
                else:
                    # All users assigned to this project
                    assignments = db.get_project_assignments(project_id) if hasattr(db, 'get_project_assignments') else []
                    targets = [a.get('user_email') for a in assignments if a.get('user_email')]
                    if not targets:
                        # Fallback: use all authors from git log on any branch
                        targets = []

                results = []
                for email in targets:
                    summary = tracker.backfill_user_history(
                        project_id=project_id,
                        project_path=project_path,
                        user_email=email,
                        since_days=int(data.get("since_days", 365))
                    )
                    results.append({"user_email": email, **summary})
                self._json_response({"success": True, "results": results})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Assign user to project (super admin or TL only)
        if path == "/api/project-assignments":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: only admins can assign users"}, 403)
                return
            try:
                db = _get_db()

                # Get project details for email
                projects = db.get_all_projects()
                project_name = None
                for p in projects:
                    if p['id'] == data.get("project_id"):
                        project_name = p['name']
                        break

                success = db.assign_user_to_project(
                    data.get("project_id"),
                    data.get("user_email"),
                    data.get("role"),
                    _current_user.get("id")
                )

                # Send email notification to user
                if success and project_name:
                    notifier = _get_email_notifier()
                    user = db.get_user_by_email(data.get("user_email"))
                    if user:
                        notifier.send_project_assignment_notification(
                            user['email'],
                            user['name'],
                            project_name,
                            _current_user.get('name', 'Admin'),
                            data.get("role", "developer")
                        )

                # Backfill git history so analytics + branches appear immediately
                backfill_summary = None
                if success:
                    try:
                        project_path = None
                        for p in projects:
                            if p['id'] == data.get("project_id"):
                                project_path = p.get('path')
                                break
                        if project_path and os.path.exists(project_path):
                            from agent.analytics import get_tracker
                            tracker = get_tracker()
                            backfill_summary = tracker.backfill_user_history(
                                project_id=data.get("project_id"),
                                project_path=project_path,
                                user_email=data.get("user_email"),
                                since_days=365
                            )
                    except Exception as e:
                        print(f"[Assign] backfill error: {e}")

                self._json_response({"success": success, "backfill": backfill_summary})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        self._json_response({"error": "Not found"}, 404)

    def do_DELETE(self):
        """Handle DELETE requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Remove user from project assignment (super admin or TL only)
        if path.startswith("/api/project-assignments/"):
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            if _current_user.get("role") not in ("super_admin", "admin"):
                self._json_response({"error": "Forbidden: only admins can remove assignments"}, 403)
                return
            try:
                # Parse project_id and user_email from URL: /api/project-assignments/{project_id}/{user_email}
                parts = path.split("/")
                if len(parts) >= 5:
                    project_id = int(parts[4])
                    user_email = parts[5] if len(parts) > 5 else ""
                    db = _get_db()
                    success = db.remove_user_from_project(project_id, user_email)
                    self._json_response({"success": success})
                else:
                    self._json_response({"error": "Invalid URL format"}, 400)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        self._json_response({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _ensure_eslint_for_scan(self, project_root: str) -> Optional[str]:
        """Ensure ESLint is installed and configured with unused-imports plugin."""
        import subprocess
        import shutil
        from pathlib import Path
        import json
        
        # Check if ESLint is already installed
        eslint_path = Path(project_root) / "node_modules" / ".bin" / "eslint"
        eslint_cmd_path = Path(project_root) / "node_modules" / ".bin" / "eslint.cmd"
        
        eslint_bin = None
        if eslint_path.exists():
            eslint_bin = str(eslint_path)
        elif eslint_cmd_path.exists():
            eslint_bin = str(eslint_cmd_path)
        
        # Install ESLint if not found
        if not eslint_bin:
            if not shutil.which("npm"):
                print("[ESLint] npm not available, skipping ESLint")
                return None
            
            pkg_json = Path(project_root) / "package.json"
            has_typescript = any(Path(project_root).glob("**/*.ts")) or any(Path(project_root).glob("**/*.tsx"))
            
            print(f"[ESLint] Installing ESLint with unused-imports plugin...")
            packages = ["eslint", "eslint-plugin-unused-imports"]
            if has_typescript:
                packages += ["@typescript-eslint/parser", "@typescript-eslint/eslint-plugin"]
            
            result = subprocess.run(
                ["npm", "install", "--save-dev"] + packages,
                cwd=project_root,
                capture_output=True,
                text=True,
                shell=True,
            )
            if result.returncode != 0:
                print(f"[ESLint] Install failed: {result.stderr[:200]}")
                return None
            
            # Re-check for eslint binary
            if eslint_path.exists():
                eslint_bin = str(eslint_path)
            elif eslint_cmd_path.exists():
                eslint_bin = str(eslint_cmd_path)
        
        if not eslint_bin:
            return None
        
        # Ensure ESLint config exists with unused-imports rules
        eslintrc_path = Path(project_root) / ".eslintrc.json"
        has_typescript = any(Path(project_root).glob("**/*.ts")) or any(Path(project_root).glob("**/*.tsx"))
        
        # Load existing config or create new one
        if eslintrc_path.exists():
            try:
                config = json.loads(eslintrc_path.read_text())
                print(f"[ESLint] Updating existing .eslintrc.json with unused-imports rules")
            except json.JSONDecodeError:
                config = {}
                print(f"[ESLint] Existing .eslintrc.json is invalid, creating new one")
        else:
            config = {}
            print(f"[ESLint] Creating new .eslintrc.json with unused-imports rules")
        
        # Ensure plugins array exists
        if "plugins" not in config:
            config["plugins"] = []
        if "unused-imports" not in config["plugins"]:
            config["plugins"].append("unused-imports")
        
        # Ensure extends array exists
        if "extends" not in config:
            config["extends"] = []
        if isinstance(config["extends"], str):
            config["extends"] = [config["extends"]]
        if "plugin:unused-imports/recommended" not in config["extends"]:
            config["extends"].append("plugin:unused-imports/recommended")
        
        # Ensure rules object exists
        if "rules" not in config:
            config["rules"] = {}
        
        # Add unused-imports rules
        config["rules"]["unused-imports/no-unused-imports"] = "error"
        config["rules"]["unused-imports/no-unused-vars"] = [
            "warn",
            {"vars": "all", "varsIgnorePattern": "^_", "args": "after-used", "argsIgnorePattern": "^_"}
        ]
        config["rules"]["no-unused-vars"] = "off"
        
        # TypeScript support
        if has_typescript:
            if "@typescript-eslint" not in config["plugins"]:
                config["plugins"].append("@typescript-eslint")
            if "plugin:@typescript-eslint/recommended" not in config["extends"]:
                config["extends"].append("plugin:@typescript-eslint/recommended")
            config["parser"] = "@typescript-eslint/parser"
            config["rules"]["@typescript-eslint/no-unused-vars"] = [
                "warn",
                {"vars": "all", "varsIgnorePattern": "^_", "args": "after-used", "argsIgnorePattern": "^_"}
            ]
        
        # Ensure env and parserOptions exist
        if "env" not in config:
            config["env"] = {"browser": True, "es2021": True, "node": True}
        if "parserOptions" not in config:
            config["parserOptions"] = {"ecmaVersion": "latest", "sourceType": "module"}
        
        eslintrc_path.write_text(json.dumps(config, indent=2))
        print(f"[ESLint] Config updated with unused-imports rules")
        
        return eslint_bin

    def _run_eslint_json(self, project_root: str, files: List[str], temp_dir: Optional[str] = None) -> List:
        """Run ESLint with JSON output and return violations."""
        import json
        import subprocess
        from pathlib import Path
        from agent.utils.reporter import Severity, Violation
        
        violations = []
        
        # Ensure ESLint is installed and configured
        eslint_bin = self._ensure_eslint_for_scan(project_root)
        if not eslint_bin:
            return violations
        
        # Run ESLint with JSON format on all files
        try:
            js_ts_files = [f for f in files if f.endswith(('.js', '.jsx', '.ts', '.tsx'))]
            if not js_ts_files:
                return violations
            
            print(f"[ESLint] Running on {len(js_ts_files)} files...")
            print(f"[ESLint] First few files: {js_ts_files[:3]}")
            cmd = eslint_bin.split() + ["--format=json", "--no-error-on-unmatched-pattern"] + js_ts_files
            print(f"[ESLint] Command: {' '.join(cmd[:5])} ...")
            result = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, timeout=120)
            print(f"[ESLint] Exit code: {result.returncode}")
            if result.stdout:
                try:
                    eslint_results = json.loads(result.stdout)
                    total_eslint_issues = sum(len(f.get('messages', [])) for f in eslint_results)
                    print(f"[ESLint] Found {total_eslint_issues} issues from JSON output")
                    print(f"[ESLint] Results files: {[f.get('filePath', '')[-50:] for f in eslint_results[:3]]}")  # Last 50 chars of path
                    for file_result in eslint_results:
                        file_path = file_result.get("filePath", "")
                        msgs = file_result.get("messages", [])
                        if msgs:
                            print(f"[ESLint] {file_path[-50:]}: {len(msgs)} messages")
                        for msg in file_result.get("messages", []):
                            # Map ESLint severity to our severity
                            # 2 = error, 1 = warning, 0 = info
                            eslint_severity = msg.get("severity", 1)
                            if eslint_severity == 2:
                                severity = Severity.ERROR
                            elif eslint_severity == 1:
                                severity = Severity.WARNING
                            else:
                                severity = Severity.INFO
                            
                            rule_id = msg.get("ruleId", "eslint")
                            message = msg.get("message", "")
                            line = msg.get("line", 1)
                            
                            # Convert absolute path to relative path for consistency
                            rel_path = file_path
                            if temp_dir and file_path.startswith(temp_dir):
                                rel_path = file_path[len(temp_dir):].lstrip('/\\')
                            elif project_root and file_path.startswith(project_root):
                                rel_path = file_path[len(project_root):].lstrip('/\\')
                            
                            # Create violation with relative path
                            v = Violation(
                                rule_id=rule_id or "eslint",
                                rule_name=rule_id or "ESLint Issue",
                                severity=severity,
                                file_path=rel_path,
                                line_number=line,
                                message=message,
                                fix_suggestion=msg.get("fix", {}).get("text", "") if msg.get("fix") else "",
                                snippet="",
                                category="lint"
                            )
                            violations.append(v)
                except json.JSONDecodeError:
                    print(f"[ESLint] Failed to parse JSON output")
            if result.stderr:
                print(f"[ESLint] stderr: {result.stderr[:200]}")
        except Exception as e:
            print(f"[ESLint] Error running ESLint: {e}")
        
        return violations

    def _scan_project(self, project_path: str, project_id: int, user_email: str, strip_base: str = None) -> dict:
        """Run a code review scan on a project and return results.

        Args:
            strip_base: If provided, strip this directory prefix from all file
                        paths in the response (used when called from branch scan
                        with an external temp directory).
        """
        import tempfile
        import shutil
        import subprocess

        temp_dir = None
        scan_path = project_path
        
        try:
            # Check if it's a remote Git URL
            if project_path.startswith(('http://', 'https://', 'git@')):
                # Clone to temp directory
                temp_dir = tempfile.mkdtemp(prefix='cra_scan_')
                repo_name = project_path.split('/')[-1].replace('.git', '')
                scan_path = temp_dir
                
                # Clone the repository
                result = subprocess.run(
                    ['git', 'clone', '--depth', '1', project_path, temp_dir],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    return {"success": False, "error": f"Failed to clone repository: {result.stderr}"}
            elif not os.path.exists(project_path):
                return {"success": False, "error": f"Project path does not exist: {project_path}"}
            
            from agent.detector.language_detector import LanguageDetector
            from agent.detector.framework_detector import FrameworkDetector
            from agent.git.git_utils import scan_directory
            from agent.utils.config_manager import ConfigManager
            from agent.rules.rule_loader import RuleLoader
            from agent.rules.rule_engine import RuleEngine
            from agent.analyzer.cross_file_analyzer import (
                detect_cross_file_duplicates,
                detect_cross_file_constants,
                detect_missing_test_files,
                detect_architecture_issues,
            )
            from agent.analyzer.python_analyzer import PythonAnalyzer
            from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer
            from agent.utils.reporter import ReviewResult
            from datetime import date
            
            # Detect language and framework
            config = ConfigManager()
            lang = LanguageDetector(scan_path).detect_primary_language()
            fw = FrameworkDetector(scan_path).detect()
            
            # Scan files
            files = scan_directory(scan_path, lang, list(config.exclude_paths))
            
            if not files:
                return {
                    "success": True,
                    "project_name": project_path.split('/')[-1] or project_path.split('\\')[-1],
                    "files": [],
                    "violations": [],
                    "summary": {"errors": 0, "warnings": 0, "infos": 0, "total": 0}
                }
            
            # Load rules and run analysis
            loader = RuleLoader()
            rules = loader.load_rules(language=lang, framework=fw)
            print(f"[Scan] Loaded {len(rules)} rules")
            ast_rules = [r for r in rules if r.get('type') == 'ast']
            print(f"[Scan] AST rules: {[r.get('id') for r in ast_rules]}")
            engine = RuleEngine(python_analyzer=PythonAnalyzer(), js_analyzer=JavaScriptAnalyzer())
            result = engine.review_files(files, rules, config.max_file_size_bytes, config.exclude_paths)
            print(f"[Scan] Rule engine found {len(result.violations)} violations")
            
            # Run cross-file analysis (like old dashboard)
            dup_violations, dup_stats = detect_cross_file_duplicates(files, lang)
            const_violations = detect_cross_file_constants(files, lang)
            test_violations = detect_missing_test_files(files, scan_path, lang)
            arch_violations = detect_architecture_issues(scan_path, lang, fw, files)
            
            result.violations.extend(dup_violations)
            result.violations.extend(const_violations)
            result.violations.extend(test_violations)
            result.violations.extend(arch_violations)
            
            print(f"[Scan] Duplication stats: {dup_stats.duplicated_lines} / {dup_stats.total_lines} lines ({dup_stats.percentage:.1f}%)")

            # Run taint analysis for Python projects (matches CLI behavior)
            if lang == 'python':
                try:
                    from agent.analyzer.taint_analyzer import run_taint_analysis
                    for f in files:
                        if f.endswith('.py'):
                            try:
                                src = Path(f).read_text(encoding='utf-8', errors='replace')
                                taint_v = run_taint_analysis(f, src)
                                result.violations.extend(taint_v)
                            except OSError:
                                pass
                    print(f"[Scan] Taint analysis complete")
                except Exception as e:
                    print(f"[Scan] Taint analysis skipped: {e}")

            # Run ESLint for JS/TS projects to catch linting issues
            eslint_violations = []
            if lang in ('javascript', 'typescript'):
                eslint_violations = self._run_eslint_json(scan_path, files, temp_dir)
                print(f"[Scan] ESLint found {len(eslint_violations)} violations")
                for v in eslint_violations[:5]:  # Log first 5
                    print(f"  - {v.file_path}:{v.line_number} [{v.severity.value}] {v.rule_id}: {v.message[:50]}")
                result.violations.extend(eslint_violations)
            
            print(f"[Scan] Before dedup: {len(result.violations)} total violations")
            result.deduplicate()
            print(f"[Scan] After dedup: {len(result.violations)} total violations")
            
            # Calculate relative paths to hide temp directory
            # Use strip_base (from branch scan) or temp_dir (from URL clone) or scan_path
            base_to_strip = strip_base or temp_dir or scan_path

            def make_relative(path):
                norm_path = path.replace('\\', '/')
                norm_base = base_to_strip.replace('\\', '/').rstrip('/') + '/' if base_to_strip else ""
                if norm_base and norm_path.startswith(norm_base):
                    return norm_path[len(norm_base):]
                # Also try without trailing slash
                norm_base2 = norm_base.rstrip('/')
                if norm_base2 and norm_path.startswith(norm_base2):
                    return norm_path[len(norm_base2):].lstrip('/')
                return path.replace('\\', '/')
            
            # Prepare file list with relative paths
            file_list = []
            for f in files:
                rel_path = make_relative(f)
                file_list.append({
                    "name": rel_path.split('/')[-1] or rel_path.split('\\')[-1], 
                    "path": rel_path
                })
            
            # Prepare violations with relative paths
            violations = []
            for v in result.violations:
                rel_file = make_relative(v.file_path)
                # Check if this file is in our file list
                file_exists = any(f['path'] == rel_file for f in file_list)
                if not file_exists:
                    print(f"[Scan] Warning: Violation file not in file list: {rel_file} (original: {v.file_path})")
                violations.append({
                    "file": rel_file,
                    "line": v.line_number,
                    "severity": v.severity.value if hasattr(v.severity, 'value') else str(v.severity),
                    "message": v.message,
                    "rule_id": v.rule_id,
                    "code_snippet": v.snippet if v.snippet else None,
                    "fix_suggestion": v.fix_suggestion if hasattr(v, 'fix_suggestion') else None
                })
            
            # Debug: Log sample violations
            print(f"[Scan] Sample violations being returned:")
            for v in violations[:5]:
                print(f"  - {v['file']}:{v['line']} [{v['severity']}] {v['rule_id']}: {v['message'][:50]}")
            
            # Debug: Check for ESLint unused variable violations
            unused_vars = [v for v in violations if 'unused' in v['rule_id'].lower()]
            print(f"[Scan] Unused variable violations: {len(unused_vars)}")
            for v in unused_vars[:5]:
                print(f"  - {v['file']}:{v['line']} {v['rule_id']}")
            
            # Debug: Sample file paths
            print(f"[Scan] Sample file paths in file_list:")
            for f in file_list[:5]:
                print(f"  - {f['path']}")
            
            # Update analytics with scan results
            db = _get_db()
            quality_score = max(0, 100 - len([v for v in violations if v["severity"] == "error"]) * 5 - len([v for v in violations if v["severity"] == "warning"]) * 2)
            db.log_analytics(
                user_email=user_email,
                project_id=project_id,
                date=date.today(),
                commits_count=0,  # Not a commit, just a scan
                issues_found=len(violations),
                code_quality_score=quality_score,
                effort_score=0
            )
            
            return {
                "success": True,
                "project_name": project_path.split('/')[-1] or project_path.split('\\')[-1],
                "language": lang,
                "framework": fw,
                "files": file_list,
                "violations": violations,
                "summary": {
                    "errors": len([v for v in violations if v["severity"] == "error"]),
                    "warnings": len([v for v in violations if v["severity"] == "warning"]),
                    "infos": len([v for v in violations if v["severity"] == "info"]),
                    "total": len(violations)
                },
                "duplication": {
                    "percentage": dup_stats.percentage,
                    "duplicated_lines": dup_stats.duplicated_lines,
                    "total_lines": dup_stats.total_lines
                }
            }

        except Exception as e:
            print(f"[Scan Error] {e}")
            return {"success": False, "error": str(e)}
        finally:
            # Clean up temp directory if we cloned
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass

    def _scan_project_branch(self, project_path: str, project_id: int, user_email: str, branch: str = "main") -> dict:
        """Run a code review scan on a specific branch of a project."""
        import tempfile
        import shutil
        import subprocess
        
        temp_dir = None
        scan_path = project_path
        
        try:
            # Check if it's a remote Git URL
            if project_path.startswith(('http://', 'https://', 'git@')):
                # Clone specific branch to temp directory
                safe_branch = branch.replace('/', '_')
                temp_dir = tempfile.mkdtemp(prefix=f'cra_scan_{safe_branch}_')
                scan_path = temp_dir
                
                # Clone the specific branch
                result = subprocess.run(
                    ['git', 'clone', '--depth', '1', '--branch', branch, project_path, temp_dir],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    # If branch doesn't exist, fall back to main and try to checkout
                    result = subprocess.run(
                        ['git', 'clone', '--depth', '1', project_path, temp_dir],
                        capture_output=True, text=True, timeout=60
                    )
                    if result.returncode != 0:
                        return {"success": False, "error": f"Failed to clone repository: {result.stderr}"}
                    
                    # Try to checkout the specific branch
                    checkout_result = subprocess.run(
                        ['git', '-C', temp_dir, 'checkout', '-b', branch, f'origin/{branch}'],
                        capture_output=True, text=True, timeout=30
                    )
                    if checkout_result.returncode != 0 and branch != 'main':
                        # Branch doesn't exist, return error or fall back to main
                        return {"success": False, "error": f"Branch '{branch}' not found in repository"}
                        
            elif os.path.exists(project_path):
                # For local repos, try to checkout the branch
                result = subprocess.run(
                    ['git', '-C', project_path, 'checkout', branch],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode != 0 and branch != 'main':
                    return {"success": False, "error": f"Branch '{branch}' not found in local repository"}
                scan_path = project_path
            else:
                return {"success": False, "error": f"Project path does not exist: {project_path}"}
            
            # Use the existing _scan_project logic but with the branch-specific path
            # Pass temp_dir as strip_base so file paths are properly relativized
            result = self._scan_project(scan_path, project_id, user_email, strip_base=temp_dir or scan_path)
            
            # Add branch info to result
            if isinstance(result, dict) and result.get("success"):
                result["branch"] = branch
            
            return result
            
        except Exception as e:
            print(f"[Scan Branch Error] {e}")
            return {"success": False, "error": str(e)}
        finally:
            # Clean up temp directory if we cloned
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass

    def _json_response(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Prevent browser caching on ALL responses (HTML, JS, JSON)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format, *args):
        # Suppress noisy request logs
        pass


def _kill_port(port: int) -> None:
    """Kill any existing process listening on the given port (prevents zombie servers)."""
    import subprocess
    import platform
    try:
        if platform.system() == "Windows":
            # Find PIDs listening on the port
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f"127.0.0.1:{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    # Don't kill our own process
                    if pid != str(os.getpid()):
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
                        print(f"  Stopped old dashboard (PID {pid}) on port {port}")
        else:
            # Unix: use lsof
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5
            )
            for pid in result.stdout.strip().split():
                if pid and pid != str(os.getpid()):
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                    print(f"  Stopped old dashboard (PID {pid}) on port {port}")
    except Exception:
        pass  # best-effort — don't let this block startup


def run_dashboard(project_dir: Optional[str] = None, port: int = 9090,
                  language: Optional[str] = None,
                  framework: Optional[str] = None,
                  no_open: bool = False,
                  mode: str = 'developer',
                  branch: Optional[str] = None,
                  repo_url: Optional[str] = None) -> int:
    """Run the dashboard server.

    Args:
        project_dir: Project directory to scan (None for multi-user admin mode)
        port: Server port
        language: Override language detection
        framework: Override framework detection
        no_open: Don't auto-open browser
        mode: 'developer' for normal dashboard, 'admin' for multi-user admin panel
        branch: Git branch (auto-detected from cwd if not given)
        repo_url: Git remote origin URL (auto-detected from cwd if not given)

    For multi-user mode:
        1. Initialize database
        2. Start HTTP server
        3. User logs in via web UI
    """
    global _scan_result, _mode, _current_user, _launch_context
    _mode = mode

    # Store launch context so the frontend can auto-navigate
    _launch_context = {
        "project_dir": project_dir,
        "branch": branch,
        "repo_url": repo_url,
    }

    if mode in ('admin', 'developer'):
        # Multi-user mode - initialize database
        print(f"\n  Starting CRA Multi-User Dashboard")
        print(f"  Mode: {mode.upper()}")
        print(f"  Initializing database...")

        try:
            db = _get_db()
            db.init_schema()
            is_first = db.is_first_run()

            if is_first:
                print(f"\n  🎉 First run! Database initialized.")
                print(f"  Open http://localhost:{port} to create Super Admin account.")
            else:
                user_count = len(db.get_all_users())
                print(f"  ✓ Database ready ({user_count} users)")
        except Exception as e:
            print(f"\n  ⚠️ Database error: {e}")
            print(f"  Make sure PostgreSQL is running and accessible.")
            return 1
    else:
        # Single-user mode - scan project
        if not project_dir:
            print("[ERROR] project_dir required for single-user mode")
            return 2

        print(f"\n  Scanning {project_dir} ...")
        result = _run_scan(project_dir, language, framework)
        with _scan_lock:
            _scan_result = result

        errs = result["summary"]["errors"]
        warns = result["summary"]["warnings"]
        infos = result["summary"]["infos"]
        total = result["summary"]["total"]

        print(f"  Found {total} issue(s): {errs} error(s), {warns} warning(s), {infos} info(s)")

    # Kill any existing dashboard process on the target port
    _kill_port(port)

    server = HTTPServer(("127.0.0.1", port), DashboardHandler)

    print(f"\n  Starting dashboard on http://localhost:{port}")
    print(f"  Press Ctrl+C to stop.\n")

    if not no_open:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()

    return 0
