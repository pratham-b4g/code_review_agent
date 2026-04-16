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

        # API: return source for a specific file
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

        # Get all TLs (for access request dropdown)
        if path == "/api/users/tls":
            try:
                db = _get_db()
                tls = db.get_all_users(role='admin')
                self._json_response([{"email": u["email"], "name": u["name"]} for u in tls])
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get all users (super admin only)
        if path == "/api/users":
            try:
                db = _get_db()
                users = db.get_all_users()
                self._json_response(users)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get all projects
        if path == "/api/projects":
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

        # Get project assignments (for super admin and TLs)
        if path.startswith("/api/projects/") and path.endswith("/assignments"):
            try:
                project_id = int(path.split("/")[3])
                db = _get_db()
                assignments = db.get_project_assignments(project_id)
                self._json_response(assignments)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get user's project assignments
        if path == "/api/user-project-assignments":
            qs = parse_qs(parsed.query)
            user_email = qs.get("user_email", [None])[0]
            if not user_email:
                self._json_response({"error": "user_email required"}, 400)
                return
            try:
                db = _get_db()
                # Get projects assigned to this user
                projects = db.get_user_projects(user_email)
                # Return as assignment objects
                assignments = [{"project_id": p["id"], "user_email": user_email, "role_on_project": p.get("role_on_project", "developer")} for p in projects]
                self._json_response(assignments)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Scan project for code review
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
                
                # Check if user is assigned to this project
                user_projects = db.get_user_projects(_current_user["email"])
                if not any(up["id"] == project_id for up in user_projects):
                    self._json_response({"error": "Not assigned to this project"}, 403)
                    return
                
                # Run code review scan
                project_path = project["path"]
                result = self._scan_project(project_path, project_id, _current_user["email"])
                self._json_response(result)
                
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get access requests
        if path == "/api/access-requests":
            qs = parse_qs(parsed.query)
            tl_email = qs.get("tl_email", [None])[0]
            try:
                db = _get_db()
                requests = db.get_pending_access_requests(tl_email)
                self._json_response(requests)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get analytics data
        if path == "/api/analytics":
            qs = parse_qs(parsed.query)
            user_email = qs.get("user_email", [None])[0]
            project_id = qs.get("project_id", [None])[0]
            days = int(qs.get("days", ["7"])[0])

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
                    days=days
                )
                self._json_response(summary)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Get detailed analytics (time series data)
        if path == "/api/analytics/detail":
            qs = parse_qs(parsed.query)
            user_email = qs.get("user_email", [None])[0]
            project_id = qs.get("project_id", [None])[0]
            days = int(qs.get("days", ["30"])[0])

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

        # Create access request
        if path == "/api/access-requests":
            try:
                db = _get_db()
                success = db.create_access_request(
                    data.get("requester_email"),
                    data.get("requester_name"),
                    data.get("tl_email")
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

        # Respond to access request
        if path.startswith("/api/access-requests/") and path.endswith("/respond"):
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

                success = db.respond_to_access_request(
                    request_id,
                    data.get("status"),
                    data.get("responded_by")
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

        # Create user (super admin only)
        if path == "/api/users":
            if not _current_user or _current_user.get("role") != "super_admin":
                self._json_response({"error": "Unauthorized"}, 403)
                return
            try:
                db = _get_db()
                success = db.create_user(
                    data.get("email"),
                    data.get("name"),
                    data.get("role"),
                    _current_user.get("id")
                )
                self._json_response({"success": success})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Create project (super admin or TL)
        if path == "/api/projects":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
                return
            try:
                db = _get_db()
                project_id = db.create_project(
                    data.get("name"),
                    data.get("path"),
                    data.get("main_branch", "main"),
                    _current_user.get("id")
                )
                self._json_response({"success": project_id is not None, "project_id": project_id})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        # Assign user to project
        if path == "/api/project-assignments":
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
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

                self._json_response({"success": success})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        self._json_response({"error": "Not found"}, 404)

    def do_DELETE(self):
        """Handle DELETE requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Remove user from project assignment
        if path.startswith("/api/project-assignments/"):
            if not _current_user:
                self._json_response({"error": "Unauthorized"}, 401)
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

    def _scan_project(self, project_path: str, project_id: int, user_email: str) -> dict:
        """Run a code review scan on a project and return results."""
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
            engine = RuleEngine(python_analyzer=PythonAnalyzer(), js_analyzer=JavaScriptAnalyzer())
            result = engine.review_files(files, rules, config.max_file_size_bytes, config.exclude_paths)
            
            # Prepare file list
            file_list = [{"name": f.split('/')[-1] or f.split('\\')[-1], "path": f} for f in files]
            
            # Prepare violations
            violations = []
            for v in result.violations:
                violations.append({
                    "file": v.file_path,
                    "line": v.line_number,
                    "severity": v.severity.value if hasattr(v.severity, 'value') else str(v.severity),
                    "message": v.message,
                    "rule_id": v.rule_id,
                    "code_snippet": v.snippet if v.snippet else None
                })
            
            # Update analytics with scan results
            db = _get_db()
            quality_score = max(0, 100 - len([v for v in violations if v["severity"] == "error"]) * 5 - len([v for v in violations if v["severity"] == "warning"]) * 2)
            db.log_analytics(
                user_email=user_email,
                project_id=project_id,
                date=date.today(),
                commits=0,  # Not a commit, just a scan
                issues=len(violations),
                quality_score=quality_score,
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
                  mode: str = 'developer') -> int:
    """Run the dashboard server.

    Args:
        project_dir: Project directory to scan (None for multi-user admin mode)
        port: Server port
        language: Override language detection
        framework: Override framework detection
        no_open: Don't auto-open browser
        mode: 'developer' for normal dashboard, 'admin' for multi-user admin panel

    For multi-user mode:
        1. Initialize database
        2. Start HTTP server
        3. User logs in via web UI
    """
    global _scan_result, _mode, _current_user
    _mode = mode

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
