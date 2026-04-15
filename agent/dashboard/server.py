"""Lightweight HTTP server that serves the CRA dashboard.

No external dependencies — uses only the Python standard library
(http.server + json).  The review is run on-demand via the /api/scan
endpoint and results are served as JSON.
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


def _run_scan(project_dir: str, language: Optional[str] = None,
              framework: Optional[str] = None) -> Dict[str, Any]:
    """Run a full review and return JSON-serialisable results."""
    from agent.detector.language_detector import LanguageDetector
    from agent.detector.framework_detector import FrameworkDetector
    from agent.git.git_utils import scan_directory
    from agent.utils.config_manager import ConfigManager
    from agent.rules.rule_loader import RuleLoader
    from agent.rules.rule_engine import RuleEngine
    from agent.analyzer.cross_file_analyzer import (
        detect_cross_file_duplicates,
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
    test_violations = detect_missing_test_files(files, project_dir, lang)
    arch_violations = detect_architecture_issues(project_dir, lang, fw, files)
    result.violations.extend(dup_violations)
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

        # Static files
        if path == "/" or path == "":
            self.path = "/index.html"

        return super().do_GET()

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


def run_dashboard(project_dir: str, port: int = 9090,
                  language: Optional[str] = None,
                  framework: Optional[str] = None,
                  no_open: bool = False) -> int:
    """Run the dashboard server.

    1. Scan the project
    2. Start HTTP server
    3. Open browser
    """
    global _scan_result

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
