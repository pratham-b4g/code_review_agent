"""Analytics tracker for monitoring developer activity."""
import json
import os
import re
import subprocess
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path

from agent.database import DatabaseManager


class AnalyticsTracker:
    """Tracks developer activity from git history and code reviews."""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    def get_git_email(self, project_path: str) -> str:
        """Get git user email from project."""
        try:
            result = subprocess.run(
                ["git", "config", "user.email"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def analyze_commit(self, project_path: str, commit_hash: str = "HEAD") -> Dict[str, Any]:
        """Analyze a single commit for metrics."""
        try:
            # Get commit stats
            result = subprocess.run(
                ["git", "show", "--stat", "--format=%H|%an|%ae|%ad", commit_hash],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=10
            )

            lines = result.stdout.strip().split('\n')
            if not lines:
                return {}

            # Parse header
            header = lines[0].split('|')
            if len(header) < 4:
                return {}

            commit_data = {
                'hash': header[0],
                'author_name': header[1],
                'author_email': header[2],
                'date': header[3],
                'files_changed': 0,
                'insertions': 0,
                'deletions': 0
            }

            # Parse stats from last line
            if lines:
                last_line = lines[-1]
                # Match patterns like "5 files changed, 100 insertions(+), 20 deletions(-)"
                files_match = re.search(r'(\d+) file', last_line)
                insertions_match = re.search(r'(\d+) insertion', last_line)
                deletions_match = re.search(r'(\d+) deletion', last_line)

                if files_match:
                    commit_data['files_changed'] = int(files_match.group(1))
                if insertions_match:
                    commit_data['insertions'] = int(insertions_match.group(1))
                if deletions_match:
                    commit_data['deletions'] = int(deletions_match.group(1))

            return commit_data
        except Exception as e:
            print(f"[Analytics] Error analyzing commit: {e}")
            return {}

    def get_commits_for_date(self, project_path: str, target_date: date,
                             author_email: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all commits for a specific date."""
        try:
            date_str = target_date.strftime('%Y-%m-%d')
            since = f"{date_str} 00:00:00"
            until = f"{date_str} 23:59:59"

            cmd = [
                "git", "log",
                f"--since={since}",
                f"--until={until}",
                "--format=%H|%an|%ae|%ad",
                "--no-merges"
            ]

            if author_email:
                cmd.extend(["--author", author_email])

            result = subprocess.run(
                cmd,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=30
            )

            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) >= 4:
                    commits.append({
                        'hash': parts[0],
                        'author_name': parts[1],
                        'author_email': parts[2],
                        'date': parts[3]
                    })

            # Get stats for each commit
            for commit in commits:
                stats = self.analyze_commit(project_path, commit['hash'])
                commit.update(stats)

            return commits
        except Exception as e:
            print(f"[Analytics] Error getting commits: {e}")
            return []

    def analyze_code_quality(self, project_path: str, files: List[str]) -> Dict[str, int]:
        """Run code review and count issues."""
        from agent.detector.language_detector import LanguageDetector
        from agent.utils.config_manager import ConfigManager
        from agent.rules.rule_loader import RuleLoader
        from agent.rules.rule_engine import RuleEngine
        from agent.analyzer.python_analyzer import PythonAnalyzer
        from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer

        try:
            config = ConfigManager()
            lang = LanguageDetector(project_path).detect_primary_language()

            loader = RuleLoader()
            rules = loader.load_rules(language=lang, framework=None)
            engine = RuleEngine(python_analyzer=PythonAnalyzer(), js_analyzer=JavaScriptAnalyzer())
            result = engine.review_files(files, rules, config.max_file_size_bytes, config.exclude_paths)

            return {
                'total_issues': len(result.violations),
                'errors': len(result.errors),
                'warnings': len(result.warnings),
                'infos': len(result.infos)
            }
        except Exception as e:
            print(f"[Analytics] Error analyzing code quality: {e}")
            return {'total_issues': 0, 'errors': 0, 'warnings': 0, 'infos': 0}

    def calculate_effort_score(self, commits: List[Dict[str, Any]],
                                issues_found: int, issues_fixed: int = 0) -> float:
        """Calculate effort score based on activity and quality."""
        if not commits:
            return 0.0

        total_lines = sum(c.get('insertions', 0) + c.get('deletions', 0) for c in commits)
        total_files = sum(c.get('files_changed', 0) for c in commits)
        commit_count = len(commits)

        # Base score from activity
        score = min(commit_count * 10, 50)  # Max 50 from commits
        score += min(total_files * 2, 20)  # Max 20 from files
        score += min(total_lines / 10, 20)  # Max 20 from lines

        # Quality penalty/bonus
        if issues_found == 0:
            score += 10  # Clean code bonus
        else:
            score -= min(issues_found * 2, 20)  # Max 20 penalty for issues

        # Bonus for fixing bugs
        score += min(issues_fixed * 5, 15)

        return max(0.0, min(100.0, score))

    def calculate_quality_score(self, violations_count: int, total_lines: int) -> float:
        """Calculate code quality score (0-100)."""
        if total_lines == 0:
            return 100.0

        # Issues per 100 lines
        density = (violations_count / total_lines) * 100

        # Score decreases as density increases
        if density == 0:
            return 100.0
        elif density < 1:
            return 95.0
        elif density < 3:
            return 85.0
        elif density < 5:
            return 70.0
        elif density < 10:
            return 50.0
        else:
            return max(0.0, 100 - density * 5)

    def list_project_branches(self, project_path: str) -> List[str]:
        """List all local/remote branches for a project path (local clone)."""
        if not project_path or not os.path.exists(project_path):
            return []
        branches = set()
        try:
            # Remote branches
            result = subprocess.run(
                ["git", "-C", project_path, "branch", "-r"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line or "->" in line:
                        continue
                    b = line.replace("origin/", "", 1)
                    if b:
                        branches.add(b)
            # Local branches
            result = subprocess.run(
                ["git", "-C", project_path, "branch"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    b = line.replace("*", "").strip()
                    if b:
                        branches.add(b)
        except Exception as e:
            print(f"[Analytics] list_project_branches error: {e}")
        return sorted(branches)

    def get_commits_for_user_on_branch(self, project_path: str, branch: str,
                                        user_email: str, since_days: int = 365) -> List[Dict[str, Any]]:
        """Get all commits by a user on a specific branch, grouped per-commit with stats.

        Author-match strategy: try the full email first, then the email's
        local-part (e.g. `devendra` from `devendra@b1zgroup.com`), and
        finally fetch the name aliases stored in our DB (if any). This
        handles devs whose git config uses a different email than their
        dashboard login (a very common real-world mismatch).
        """
        if not project_path or not os.path.exists(project_path):
            return []
        try:
            since = f"{since_days}.days.ago"
            ref = f"origin/{branch}" if branch else "HEAD"
            # Fall back to local branch name if remote ref doesn't exist
            check = subprocess.run(
                ["git", "-C", project_path, "rev-parse", "--verify", ref],
                capture_output=True, text=True, timeout=10
            )
            if check.returncode != 0:
                ref = branch
                check = subprocess.run(
                    ["git", "-C", project_path, "rev-parse", "--verify", ref],
                    capture_output=True, text=True, timeout=10
                )
                if check.returncode != 0:
                    return []

            # Build candidate author patterns (git --author is a regex OR'd
            # against name+email substrings — use --perl-regexp for safety).
            candidates: List[str] = []
            if user_email:
                candidates.append(user_email)
                local_part = user_email.split('@', 1)[0]
                if local_part and local_part != user_email:
                    candidates.append(local_part)
            # Try aliases from DB (user's display name, etc.)
            try:
                u = self.db.get_user_by_email(user_email) if hasattr(self.db, 'get_user_by_email') else None
                if u and u.get('name'):
                    candidates.append(u['name'])
            except Exception:
                pass

            commits: List[Dict[str, Any]] = []
            seen_shas = set()
            # Run git log once per candidate; short-circuit after the first
            # productive pattern but always merge unique results so multiple
            # aliases combine (e.g. dev used personal email for some commits).
            for pattern in candidates:
                cmd = [
                    "git", "-C", project_path, "log", ref,
                    f"--author={pattern}",
                    f"--since={since}",
                    "--no-merges",
                    "--pretty=format:%H|%an|%ae|%ad",
                    "--date=short",
                    "--shortstat",
                    "-i",  # case-insensitive author match
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    continue
                self._merge_git_log_output(result.stdout, commits, seen_shas)
                # If the full email already produced results, don't dilute
                # with a looser pattern — stops "devendra" matching someone
                # else named Devendra.
                if commits and pattern == user_email:
                    return commits
            return commits
        except Exception as e:
            print(f"[Analytics] get_commits_for_user_on_branch error on {branch}: {e}")
            return []

    def _merge_git_log_output(self, stdout: str, commits: List[Dict[str, Any]],
                               seen_shas: set) -> None:
        """Parse `git log --shortstat` output and append unique commits."""
        try:
            lines = stdout.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line:
                    i += 1
                    continue
                if "|" in line and line.count("|") >= 3:
                    parts = line.split("|")
                    sha = parts[0]
                    if sha in seen_shas:
                        # Skip body + possible shortstat so we don't mis-align
                        if i + 1 < len(lines) and ("changed" in lines[i + 1] or
                                                   "insertion" in lines[i + 1] or
                                                   "deletion" in lines[i + 1]):
                            i += 1
                        i += 1
                        continue
                    seen_shas.add(sha)
                    entry = {
                        "hash": sha,
                        "author_name": parts[1],
                        "author_email": parts[2],
                        "date": parts[3],
                        "files_changed": 0,
                        "insertions": 0,
                        "deletions": 0,
                    }
                    # Look ahead for shortstat line
                    if i + 1 < len(lines):
                        stat_line = lines[i + 1].strip()
                        if "changed" in stat_line or "insertion" in stat_line or "deletion" in stat_line:
                            files_m = re.search(r"(\d+) file", stat_line)
                            ins_m = re.search(r"(\d+) insertion", stat_line)
                            del_m = re.search(r"(\d+) deletion", stat_line)
                            if files_m:
                                entry["files_changed"] = int(files_m.group(1))
                            if ins_m:
                                entry["insertions"] = int(ins_m.group(1))
                            if del_m:
                                entry["deletions"] = int(del_m.group(1))
                            i += 1  # consume stat line
                    commits.append(entry)
                i += 1
        except Exception as e:
            print(f"[Analytics] _merge_git_log_output parse error: {e}")

    # ──────────────────────────────────────────────────────────────
    # Git-derived developer activity (authoritative source of truth)
    # ──────────────────────────────────────────────────────────────

    def ensure_local_clone(self, project_path: str, project_id: Optional[int] = None) -> Optional[str]:
        """Return a LOCAL path to the repo so git commands work.

        - If `project_path` is already a valid local git repo, returns it as-is.
        - If it's a remote URL (http/https/git@), clones (or fetches) into a
          persistent cache dir keyed by project_id (or URL hash) so analytics
          has a long-lived copy across restarts.
        Returns None on failure.
        """
        if not project_path:
            return None

        # Case 1: local directory that exists and is a git repo
        if os.path.exists(project_path) and os.path.isdir(os.path.join(project_path, ".git")):
            return project_path
        if os.path.exists(project_path):
            # Exists but not a git repo — nothing we can do
            return project_path if os.path.isdir(project_path) else None

        # Case 2: remote URL — cache clone
        is_remote = project_path.startswith(("http://", "https://", "git@", "ssh://"))
        if not is_remote:
            return None

        try:
            import hashlib
            key = str(project_id) if project_id is not None else hashlib.sha1(
                project_path.encode("utf-8")
            ).hexdigest()[:12]
            cache_root = Path.home() / ".cra_cache" / "projects"
            cache_root.mkdir(parents=True, exist_ok=True)
            clone_dir = cache_root / f"project_{key}"

            if clone_dir.exists() and (clone_dir / ".git").exists():
                # Already cloned — just refresh refs
                subprocess.run(
                    ["git", "-C", str(clone_dir), "fetch", "--all", "--prune"],
                    capture_output=True, text=True, timeout=120
                )
                return str(clone_dir)

            # Fresh clone (full, not shallow, so git log has full history)
            print(f"[Analytics] Cloning {project_path} → {clone_dir}")
            result = subprocess.run(
                ["git", "clone", "--no-single-branch", project_path, str(clone_dir)],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                print(f"[Analytics] Clone failed: {result.stderr[:200]}")
                return None
            return str(clone_dir)
        except Exception as e:
            print(f"[Analytics] ensure_local_clone error: {e}")
            return None

    def get_files_touched_by_user(self, project_path: str, user_email: str,
                                   since_days: int = 7,
                                   branch: Optional[str] = None) -> set:
        """Return a SET of normalized file paths modified by the user.

        Uses `git log --name-only --author=<email>`. When branch is given,
        limits log to that branch; otherwise scans across the repo.
        """
        if not project_path or not os.path.exists(project_path):
            return set()
        try:
            since = f"{since_days}.days.ago"
            # Same alias fallback chain as get_commits_for_user_on_branch
            candidates: List[str] = []
            if user_email:
                candidates.append(user_email)
                lp = user_email.split('@', 1)[0]
                if lp and lp != user_email:
                    candidates.append(lp)
            try:
                u = self.db.get_user_by_email(user_email) if hasattr(self.db, 'get_user_by_email') else None
                if u and u.get('name'):
                    candidates.append(u['name'])
            except Exception:
                pass

            ref = None
            if branch:
                ref_check = subprocess.run(
                    ["git", "-C", project_path, "rev-parse", "--verify", f"origin/{branch}"],
                    capture_output=True, text=True, timeout=5
                )
                ref = f"origin/{branch}" if ref_check.returncode == 0 else branch

            files: set = set()
            for pattern in candidates:
                base = ["git", "-C", project_path, "log"]
                if ref:
                    base.append(ref)
                base += [f"--author={pattern}", f"--since={since}",
                         "--no-merges", "--name-only", "--pretty=format:", "-i"]
                result = subprocess.run(base, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    continue
                for line in result.stdout.splitlines():
                    p = line.strip().replace("\\", "/").lstrip("./")
                    if p:
                        files.add(p)
                # If the full email already found files, don't broaden to
                # looser patterns (avoids cross-user collisions).
                if files and pattern == user_email:
                    return files
            return files
        except Exception as e:
            print(f"[Analytics] get_files_touched_by_user error: {e}")
            return set()

    def get_developer_activity(self, project_path: str, user_email: str,
                               since_days: int = 7) -> Dict[str, Any]:
        """Aggregate a developer's git activity across ALL branches.

        Returns:
            {
              "total_commits": int,            # unique SHAs authored in window
              "lines_added": int,
              "lines_removed": int,
              "files_touched": int,
              "current_branch": str|None,      # branch of most-recent commit
              "latest_commit_date": str|None,  # ISO
              "branches": [                    # per-branch breakdown
                 {"name": str, "commits": int, "last_date": str,
                  "first_date": str, "unique_commits": int}
              ]
            }

        Commits that appear on multiple branches are NOT double-counted in
        `total_commits`; each branch row shows how many of its commits were
        authored by the user (which may overlap with other branches).
        """
        empty = {
            "total_commits": 0, "lines_added": 0, "lines_removed": 0,
            "files_touched": 0, "current_branch": None,
            "latest_commit_date": None, "branches": [],
        }
        if not project_path or not os.path.exists(project_path):
            return empty

        try:
            subprocess.run(
                ["git", "-C", project_path, "fetch", "--all", "--prune"],
                capture_output=True, text=True, timeout=30
            )
        except Exception:
            pass

        all_branches = self.list_project_branches(project_path)
        if not all_branches:
            return empty

        # Track unique SHAs across branches → dedup total commits/lines
        seen_shas: Dict[str, Dict[str, Any]] = {}
        per_branch: List[Dict[str, Any]] = []

        for br in all_branches:
            commits = self.get_commits_for_user_on_branch(
                project_path, br, user_email, since_days=since_days
            )
            if not commits:
                continue
            dates = sorted([c.get("date", "") for c in commits if c.get("date")])
            last_date = dates[-1] if dates else None
            first_date = dates[0] if dates else None
            per_branch.append({
                "name": br,
                "commits": len(commits),
                "unique_commits": len({c["hash"] for c in commits if c.get("hash")}),
                "last_date": last_date,
                "first_date": first_date,
            })
            for c in commits:
                sha = c.get("hash")
                if not sha or sha in seen_shas:
                    continue
                seen_shas[sha] = c

        total_commits = len(seen_shas)
        lines_added = sum(c.get("insertions", 0) for c in seen_shas.values())
        lines_removed = sum(c.get("deletions", 0) for c in seen_shas.values())
        files_touched = len(self.get_files_touched_by_user(
            project_path, user_email, since_days=since_days
        ))

        # Current branch = branch containing the user's latest commit
        current_branch = None
        latest_date = None
        for b in per_branch:
            if b["last_date"] and (latest_date is None or b["last_date"] > latest_date):
                latest_date = b["last_date"]
                current_branch = b["name"]

        # Sort branches most-recent first
        per_branch.sort(key=lambda b: (b.get("last_date") or ""), reverse=True)

        return {
            "total_commits": total_commits,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "files_touched": files_touched,
            "current_branch": current_branch,
            "latest_commit_date": latest_date,
            "branches": per_branch,
        }

    def _build_daily_activity(self, projects_scope: List[Dict[str, Any]],
                              dev_emails, days: int) -> List[Dict[str, Any]]:
        """Return `[{date, commits, lines}]` for the last `days` days.

        Aggregated across all in-scope projects + developers. Safe when
        there's no data — returns a zero-filled series so the chart still
        renders a clean horizontal baseline instead of an empty panel.
        """
        from datetime import date, timedelta
        today = date.today()
        # Cap to ~3 months so the "All Time" filter doesn't spawn years of
        # git subprocesses; the chart is a recent-trend indicator regardless.
        window = max(1, min(int(days or 7), 90))

        buckets: Dict[str, Dict[str, int]] = {}
        for i in range(window):
            d = (today - timedelta(days=window - 1 - i)).isoformat()
            buckets[d] = {"commits": 0, "lines": 0}

        emails = [e for e in (dev_emails or []) if e]
        if not emails or not projects_scope:
            return [{"date": d, "commits": v["commits"], "lines": v["lines"]}
                    for d, v in buckets.items()]

        since = f"{window}.days.ago"
        for p in projects_scope:
            p_path = self.ensure_local_clone(p.get('path'), project_id=p.get('id'))
            if not p_path or not os.path.exists(p_path):
                continue
            for email in emails:
                # Reuse the alias-fallback chain for robustness
                candidates = [email]
                lp = email.split('@', 1)[0]
                if lp and lp != email:
                    candidates.append(lp)
                seen_shas_local = set()
                for pat in candidates:
                    try:
                        result = subprocess.run(
                            ["git", "-C", p_path, "log", "--all",
                             f"--author={pat}", f"--since={since}",
                             "--no-merges", "-i",
                             "--pretty=format:%H|%ad", "--date=short",
                             "--shortstat"],
                            capture_output=True, text=True, timeout=60,
                        )
                    except Exception:
                        continue
                    if result.returncode != 0 or not result.stdout:
                        continue
                    lines = result.stdout.splitlines()
                    i = 0
                    while i < len(lines):
                        ln = lines[i].strip()
                        if "|" in ln and ln.count("|") >= 1:
                            sha, d_str = ln.split("|", 1)
                            if sha in seen_shas_local:
                                # Skip possible shortstat
                                if i + 1 < len(lines) and ("changed" in lines[i+1] or "insertion" in lines[i+1]):
                                    i += 1
                                i += 1; continue
                            seen_shas_local.add(sha)
                            bucket = buckets.get(d_str)
                            if bucket is not None:
                                bucket["commits"] += 1
                                if i + 1 < len(lines):
                                    stat = lines[i+1]
                                    ins = re.search(r"(\d+) insertion", stat)
                                    dele = re.search(r"(\d+) deletion", stat)
                                    if ins or dele:
                                        bucket["lines"] += (int(ins.group(1)) if ins else 0) + \
                                                           (int(dele.group(1)) if dele else 0)
                                        i += 1
                        i += 1
                    # Full-email hit is enough; skip looser patterns
                    if pat == email and any(v["commits"] for v in buckets.values()):
                        break

        return [{"date": d, "commits": v["commits"], "lines": v["lines"]}
                for d, v in buckets.items()]

    def backfill_user_history(self, project_id: int, project_path: str,
                              user_email: str, since_days: int = 365) -> Dict[str, Any]:
        """Walk git history for a user across ALL branches and populate
        developer_analytics so analytics shows up immediately after a
        developer is assigned to an existing project.

        Returns a summary dict: {branches_found, commits_imported, days_logged}.
        """
        summary = {"branches_found": 0, "commits_imported": 0, "days_logged": 0,
                   "branches": [], "errors": []}
        if not project_path or not os.path.exists(project_path):
            summary["errors"].append(
                f"Project path not accessible locally for backfill: {project_path}. "
                "Clone the repo locally and re-run."
            )
            return summary

        # Ensure origin refs are up to date if this is a git repo with a remote
        try:
            subprocess.run(
                ["git", "-C", project_path, "fetch", "--all", "--prune"],
                capture_output=True, text=True, timeout=60
            )
        except Exception:
            pass  # fetch failure shouldn't block backfill

        branches = self.list_project_branches(project_path)
        summary["branches_found"] = len(branches)
        summary["branches"] = branches

        for branch in branches:
            commits = self.get_commits_for_user_on_branch(
                project_path, branch, user_email, since_days=since_days
            )
            if not commits:
                continue

            # Group commits by date → aggregate per (branch, date)
            per_day: Dict[str, Dict[str, int]] = {}
            for c in commits:
                d_str = c.get("date") or ""
                if not d_str:
                    continue
                bucket = per_day.setdefault(d_str, {
                    "commits": 0, "insertions": 0, "deletions": 0, "files": 0,
                })
                bucket["commits"] += 1
                bucket["insertions"] += c.get("insertions", 0)
                bucket["deletions"] += c.get("deletions", 0)
                bucket["files"] += c.get("files_changed", 0)

            for d_str, stats in per_day.items():
                try:
                    target_date = datetime.strptime(d_str, "%Y-%m-%d").date()
                except Exception:
                    continue
                total_lines = stats["insertions"] + stats["deletions"]
                # No quality scan for historical commits; use neutral placeholders.
                quality = 100.0
                effort = min(100.0, stats["commits"] * 10 + min(stats["files"] * 2, 20)
                             + min(total_lines / 10, 20))
                ok = self.db.log_analytics(
                    user_email=user_email,
                    project_id=project_id,
                    date=target_date,
                    branch=branch,
                    commits_count=stats["commits"],
                    lines_added=stats["insertions"],
                    lines_removed=stats["deletions"],
                    issues_found=0,
                    bugs_fixed=0,
                    files_changed=stats["files"],
                    code_quality_score=quality,
                    effort_score=effort,
                )
                if ok:
                    summary["days_logged"] += 1
            summary["commits_imported"] += len(commits)

        return summary

    def track_daily_activity(self, project_id: int, project_path: str,
                            user_email: str, target_date: Optional[date] = None) -> bool:
        """Track and store daily activity for a developer."""
        if not target_date:
            target_date = date.today()

        try:
            # Get commits for the date
            commits = self.get_commits_for_date(project_path, target_date, user_email)

            if not commits:
                # Still log a zero-activity day
                self.db.log_analytics(
                    user_email=user_email,
                    project_id=project_id,
                    date=target_date,
                    commits_count=0,
                    lines_added=0,
                    lines_removed=0,
                    issues_found=0,
                    bugs_fixed=0,
                    files_changed=0,
                    code_quality_score=100.0,
                    effort_score=0.0
                )
                return True

            # Aggregate commit stats
            total_commits = len(commits)
            total_insertions = sum(c.get('insertions', 0) for c in commits)
            total_deletions = sum(c.get('deletions', 0) for c in commits)
            total_files = sum(c.get('files_changed', 0) for c in commits)

            # Analyze code quality (on current state)
            from agent.git.git_utils import scan_directory
            from agent.detector.language_detector import LanguageDetector

            lang = LanguageDetector(project_path).detect_primary_language()
            files = scan_directory(project_path, lang, [])
            quality = self.analyze_code_quality(project_path, files)

            # Calculate scores
            total_lines = total_insertions + total_deletions
            quality_score = self.calculate_quality_score(quality['total_issues'], max(total_lines, 1))
            effort_score = self.calculate_effort_score(commits, quality['total_issues'])

            # Log to database
            self.db.log_analytics(
                user_email=user_email,
                project_id=project_id,
                date=target_date,
                commits_count=total_commits,
                lines_added=total_insertions,
                lines_removed=total_deletions,
                issues_found=quality['total_issues'],
                bugs_fixed=0,  # Would need issue tracking integration
                files_changed=total_files,
                code_quality_score=quality_score,
                effort_score=effort_score
            )

            return True
        except Exception as e:
            print(f"[Analytics] Error tracking activity: {e}")
            return False

    # Filter preset → (start_date, end_date, label, since_days_for_git)
    # All presets are computed relative to "today" in the server's local tz.
    FILTER_PRESETS = {
        'today':      ('today',      0),
        'yesterday':  ('yesterday',  1),
        '7d':         ('last 7 days',  7),
        '15d':        ('last 15 days', 15),
        '30d':        ('last 30 days', 30),
        'last_month': ('last month',   31),
        'all_time':   ('all time',     3650),  # ~10 years
    }

    @staticmethod
    def resolve_filter(filter_key: str) -> Dict[str, Any]:
        """Turn a UI filter key into concrete date bounds."""
        today = date.today()
        if filter_key == 'today':
            start, end = today, today
        elif filter_key == 'yesterday':
            y = today - timedelta(days=1)
            start, end = y, y
        elif filter_key == '7d':
            start, end = today - timedelta(days=7), today
        elif filter_key == '15d':
            start, end = today - timedelta(days=15), today
        elif filter_key == '30d':
            start, end = today - timedelta(days=30), today
        elif filter_key == 'last_month':
            # Previous calendar month, e.g. on Apr 17 → Mar 1 .. Mar 31
            first_this_month = today.replace(day=1)
            last_prev = first_this_month - timedelta(days=1)
            start = last_prev.replace(day=1)
            end = last_prev
        elif filter_key == 'all_time':
            start, end = today - timedelta(days=3650), today
        else:
            start, end = today - timedelta(days=7), today
            filter_key = '7d'
        days_span = max(1, (end - start).days + 1)
        return {
            'key': filter_key,
            'start_date': start,
            'end_date': end,
            'days': days_span,
            # For git log `--since`, we use a safe upper bound (days_span + 1)
            'since_days': days_span,
        }

    def get_analytics_summary(self, project_id: Optional[int] = None,
                             user_email: Optional[str] = None,
                             days: int = 7,
                             branch: Optional[str] = None,
                             filter_key: Optional[str] = None,
                             viewer_email: Optional[str] = None,
                             viewer_role: Optional[str] = None) -> Dict[str, Any]:
        """Team / project analytics derived from git + project_scans.

        Role-based scoping:
          * super_admin: all projects
          * admin (TL): only projects where viewer_email has role_on_project='admin'
          * developer: only their own email; all projects they are assigned to
        """
        # Resolve filter preset if given (overrides `days`)
        if filter_key:
            f = self.resolve_filter(filter_key)
            start_date = f['start_date']
            end_date = f['end_date']
            days = f['since_days']
            filter_label = f['key']
        else:
            end_date = date.today()
            start_date = end_date - timedelta(days=days)
            filter_label = f"last {days} days"

        try:
            # ── 1. Determine projects in scope (role-aware) ──────────
            projects_scope: List[Dict[str, Any]] = []
            all_projects = self.db.get_all_projects() if hasattr(self.db, 'get_all_projects') else []

            if project_id is not None:
                projects_scope = [p for p in all_projects if p.get('id') == project_id]
            elif viewer_role == 'super_admin' or viewer_role is None:
                projects_scope = list(all_projects)
            elif viewer_role == 'admin' and viewer_email:
                # TL: only projects where they are assigned as admin
                for p in all_projects:
                    assigns = self.db.get_project_assignments(p['id']) if hasattr(self.db, 'get_project_assignments') else []
                    if any(a.get('user_email') == viewer_email and a.get('role_on_project') == 'admin' for a in assigns):
                        projects_scope.append(p)
            elif viewer_role == 'developer' and viewer_email:
                # Developer: only projects they're assigned to; and clamp user_email to self
                user_email = viewer_email
                projects_scope = self.db.get_user_projects(viewer_email) if hasattr(self.db, 'get_user_projects') else []
            else:
                projects_scope = list(all_projects)

            # ── 2. Determine developers in scope ──────────────────────
            #    Union of: users assigned to scoped projects, plus `user_email` filter.
            dev_emails: Dict[str, Dict[str, Any]] = {}
            for p in projects_scope:
                assigns = self.db.get_project_assignments(p['id']) if hasattr(self.db, 'get_project_assignments') else []
                for a in assigns:
                    em = a.get('user_email')
                    if not em:
                        continue
                    if user_email and em != user_email:
                        continue
                    if em not in dev_emails:
                        dev_emails[em] = {
                            'name': a.get('name') or em,
                            'email': em,
                            'projects': {},  # project_id -> name
                        }
                    dev_emails[em]['projects'][p['id']] = p.get('name', f"project_{p['id']}")
            if user_email and user_email not in dev_emails:
                # Developer not formally assigned but explicitly requested
                u = self.db.get_user_by_email(user_email) if hasattr(self.db, 'get_user_by_email') else None
                if u:
                    dev_emails[user_email] = {
                        'name': u.get('name') or user_email,
                        'email': user_email,
                        'projects': {p['id']: p.get('name', '') for p in projects_scope},
                    }

            # ── 3. Pre-load latest scans per (project, branch) ────────
            scans_by_project: Dict[int, List[Dict[str, Any]]] = {}
            for p in projects_scope:
                scans = self.db.get_project_scans(project_id=p['id']) if hasattr(self.db, 'get_project_scans') else []
                scans_by_project[p['id']] = scans

            # ── 4. Project-level issue totals (UNION-DEDUPE minus FIXED) ──
            #   Policy: treat the most-recently-scanned branch (preferring
            #   main/master/develop) as the "current" baseline. For each file
            #   that still has issues in the current branch, use the MAX
            #   issue count for that file across all branches. Files that
            #   are fixed in the current branch (i.e. no longer listed) are
            #   EXCLUDED — the work has been merged or resolved.
            project_issue_summary: List[Dict[str, Any]] = []
            project_current_branch: Dict[int, str] = {}
            project_unified: Dict[int, Dict[str, Dict[str, int]]] = {}
            total_issues = 0
            total_errors = 0
            total_warnings = 0
            total_infos = 0

            def _load_files_with_issues(scan) -> Dict[str, Dict[str, int]]:
                raw = scan.get('files_with_issues') or {}
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except Exception:
                        raw = {}
                return raw if isinstance(raw, dict) else {}

            for p in projects_scope:
                scans = scans_by_project.get(p['id'], [])
                if not scans:
                    continue
                # Optional UI branch filter: lock to that one branch only
                scans_filt = [s for s in scans if (branch is None or s['branch'] == branch)]
                if not scans_filt:
                    continue

                # Pick the "current" branch for dedupe reference.
                # Priority: project.main_branch → common defaults → most recently scanned.
                branch_map = {s['branch']: s for s in scans_filt}
                configured = (p.get('main_branch') or '').strip()
                pref = []
                if configured:
                    pref.append(configured)
                pref += ['main', 'master', 'develop', 'dev']
                current_br = next((b for b in pref if b in branch_map), None)
                if not current_br:
                    latest = max(scans_filt, key=lambda s: str(s.get('scanned_at') or ''))
                    current_br = latest['branch']
                current_scan = branch_map[current_br]
                current_files = _load_files_with_issues(current_scan)

                # Build unified file map: UNION across ALL branches, MAX
                # per-file. A file is included if it has issues on *any*
                # branch — this prevents the contradiction where a dev's
                # attributed-issues count (measured on their active branch)
                # could exceed the project total (previously measured only
                # against the current branch).
                unified: Dict[str, Dict[str, int]] = {}
                for s in scans_filt:
                    files_here = _load_files_with_issues(s)
                    for fp, bucket in files_here.items():
                        cand = {
                            'total':    int(bucket.get('total', 0) or 0),
                            'errors':   int(bucket.get('errors', 0) or 0),
                            'warnings': int(bucket.get('warnings', 0) or 0),
                            'infos':    int(bucket.get('infos', 0) or 0),
                        }
                        prev = unified.get(fp)
                        if prev is None or cand['total'] > prev['total']:
                            unified[fp] = cand

                proj_total    = sum(b['total']    for b in unified.values())
                proj_errors   = sum(b['errors']   for b in unified.values())
                proj_warnings = sum(b['warnings'] for b in unified.values())
                proj_infos    = sum(b['infos']    for b in unified.values())

                total_issues    += proj_total
                total_errors    += proj_errors
                total_warnings  += proj_warnings
                total_infos     += proj_infos

                project_current_branch[p['id']] = current_br
                project_unified[p['id']] = unified

                project_issue_summary.append({
                    'project_id':    p['id'],
                    'project_name':  p.get('name'),
                    'current_branch': current_br,
                    'deduped_total': proj_total,
                    'deduped_errors': proj_errors,
                    'deduped_warnings': proj_warnings,
                    'deduped_infos':    proj_infos,
                    'branches': [
                        {
                            'branch':  s['branch'],
                            'issues':  int(s.get('total_issues', 0) or 0),
                            'errors':  int(s.get('errors', 0) or 0),
                            'warnings': int(s.get('warnings', 0) or 0),
                            'infos':   int(s.get('infos', 0) or 0),
                            'quality_score': float(s.get('quality_score', 0) or 0),
                            'scanned_at': (s.get('scanned_at').isoformat()
                                           if hasattr(s.get('scanned_at'), 'isoformat')
                                           else str(s.get('scanned_at'))),
                            'is_current': s['branch'] == current_br,
                        }
                        for s in scans_filt
                    ],
                })

            # ── 5. Per-developer git activity + attributed issues ────
            developers: List[Dict[str, Any]] = []
            total_unique_commits = 0
            quality_scores: List[float] = []

            for email, meta in dev_emails.items():
                dev_total_commits = 0
                dev_lines_added = 0
                dev_lines_removed = 0
                dev_files_touched = 0
                dev_branch_stats: List[Dict[str, Any]] = []
                dev_current_branch: Optional[str] = None
                dev_latest_date: Optional[str] = None
                # For "Issues" we use MAX across branches of attributed issues.
                dev_attributed_issues_max = 0
                dev_quality_weighted: List[float] = []

                for p in projects_scope:
                    if p['id'] not in meta['projects']:
                        continue
                    # Resolve remote URLs → cached local clone so git commands work
                    p_path = self.ensure_local_clone(p.get('path'), project_id=p.get('id'))
                    if not p_path or not os.path.exists(p_path):
                        continue
                    act = self.get_developer_activity(p_path, email, since_days=days)
                    dev_total_commits += act.get('total_commits', 0)
                    dev_lines_added += act.get('lines_added', 0)
                    dev_lines_removed += act.get('lines_removed', 0)
                    dev_files_touched += act.get('files_touched', 0)

                    # Track current branch across projects: pick the latest
                    if act.get('latest_commit_date'):
                        if dev_latest_date is None or act['latest_commit_date'] > dev_latest_date:
                            dev_latest_date = act['latest_commit_date']
                            dev_current_branch = act.get('current_branch')

                    # Attribute issues per-branch using THAT branch's own scan
                    # (not the project's current-branch-filtered unified map).
                    # Rationale: a dev working only on `develop` should still
                    # get credit for the issues they introduced on `develop`,
                    # even if those files don't exist on the project's
                    # configured current branch.
                    scans_map = {s['branch']: s for s in scans_by_project.get(p['id'], [])}
                    touched_union: set = set()
                    for br_info in act.get('branches', []):
                        br_name = br_info['name']
                        touched = self.get_files_touched_by_user(
                            p_path, email, since_days=days, branch=br_name
                        )
                        touched_union |= touched
                        scan = scans_map.get(br_name)
                        branch_files = _load_files_with_issues(scan) if scan else {}
                        attributed = 0
                        attr_errors = attr_warns = attr_infos = 0
                        if touched and branch_files:
                            for fp in touched:
                                bucket = branch_files.get(fp)
                                if bucket:
                                    attributed += int(bucket.get('total', 0) or 0)
                                    attr_errors += int(bucket.get('errors', 0) or 0)
                                    attr_warns += int(bucket.get('warnings', 0) or 0)
                                    attr_infos += int(bucket.get('infos', 0) or 0)
                        dev_branch_stats.append({
                            'name': br_name,
                            'project_id': p['id'],
                            'project_name': p.get('name'),
                            'commits': br_info.get('commits', 0),
                            'last_date': br_info.get('last_date'),
                            'first_date': br_info.get('first_date'),
                            'issues': attributed,
                            'errors': attr_errors,
                            'warnings': attr_warns,
                            'infos': attr_infos,
                            'quality': (float(scan['quality_score']) if scan and scan.get('quality_score') is not None else None),
                            'scanned_at': (scan.get('scanned_at').isoformat() if scan and hasattr(scan.get('scanned_at'), 'isoformat')
                                            else (str(scan.get('scanned_at')) if scan else None)),
                        })
                        if attributed > dev_attributed_issues_max:
                            dev_attributed_issues_max = attributed
                        if scan and scan.get('quality_score') is not None:
                            dev_quality_weighted.append(float(scan['quality_score']))

                    # The `get_developer_activity` call above measured
                    # files_touched against HEAD, which is often 0 when the
                    # dev never merged to the default branch. Override with
                    # the union across the branches they actually worked on.
                    if touched_union:
                        dev_files_touched = max(dev_files_touched, len(touched_union))

                total_unique_commits += dev_total_commits

                # Opt-in diagnostic: set CRA_DEBUG_ANALYTICS=1 to see why a
                # developer shows 0 commits / 0 issues (usually an email
                # mismatch between git config and dashboard login).
                if os.getenv("CRA_DEBUG_ANALYTICS") == "1":
                    print(f"[Analytics][{email}] commits={dev_total_commits} "
                          f"files_touched={dev_files_touched} "
                          f"branches_with_activity={len(dev_branch_stats)} "
                          f"attributed_issues_max={dev_attributed_issues_max}")

                # Sort branches by recency
                dev_branch_stats.sort(key=lambda b: (b.get('last_date') or ''), reverse=True)
                branches_list = [b['name'] for b in dev_branch_stats]

                # ── Quality (per developer) ──
                # Issue-density based, log-scaled so large projects don't
                # crush to 0. Denominator priority:
                #   1. files touched (from git log) — most accurate
                #   2. commits × 2 — reasonable proxy when git HEAD-scan
                #      returned 0 (happens when HEAD is on a branch the dev
                #      never committed to, a common real-world case)
                #   3. 1 — floor so we never divide by zero
                # Crucially, when there ARE attributed issues we MUST NOT
                # return 100 (the old bug showed 100% for 262 open issues).
                import math as _m
                if dev_attributed_issues_max <= 0:
                    dev_quality = 100 if dev_total_commits > 0 else 0
                else:
                    denom = max(dev_files_touched or 0, dev_total_commits * 2, 1)
                    density = dev_attributed_issues_max / denom
                    dev_quality = round(max(10.0, 100 - 18 * _m.log10(1 + density * 8)), 1)
                quality_scores.append(dev_quality)

                # ── Effort (per developer) ──
                # Commits carry the strongest signal; lines provide a
                # secondary cue. Log-damped to keep a handful of huge
                # refactors from dominating the scale.
                effort_raw = dev_total_commits * 6 + (dev_lines_added + dev_lines_removed) * 0.04
                dev_effort = round(effort_raw, 0) if effort_raw > 0 else 0

                developers.append({
                    'name': meta['name'],
                    'email': email,
                    'commits': dev_total_commits,
                    'lines_added': dev_lines_added,
                    'lines_removed': dev_lines_removed,
                    'files_touched': dev_files_touched,
                    'issues': dev_attributed_issues_max,   # MAX across branches
                    'quality_score': dev_quality,
                    'effort_score': dev_effort,
                    'current_branch': dev_current_branch,
                    'latest_commit_date': dev_latest_date,
                    'branches': branches_list,
                    'branch_count': len(branches_list),
                    'branch_stats': dev_branch_stats,
                    'projects': list(meta['projects'].values()),
                    'project_count': len(meta['projects']),
                })

            # Aggregate project-level averages from live dev values (not stored)
            avg_quality = round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else 0
            total_effort = int(sum(d.get('effort_score', 0) or 0 for d in developers))

            # ── Daily activity timeseries (for charts) ──
            # We build a per-day commits count for the window by walking git
            # log across each project (cached clones). This powers the
            # Activity Trend chart and Dashboard widgets.
            daily_activity = self._build_daily_activity(
                projects_scope, dev_emails.keys(), days
            )

            return {
                'total_commits': total_unique_commits,
                'total_issues': total_issues,                # union-dedupe minus fixed
                'total_errors': total_errors,
                'total_warnings': total_warnings,
                'total_infos': total_infos,
                'avg_quality': avg_quality,
                'avg_effort': total_effort,
                'daily_activity': daily_activity,
                'developers': developers,
                'project_summary': project_issue_summary,    # per-project + current_branch + per-branch breakdown
                'period': f"{start_date} to {end_date}",
                'filter': filter_key or f"{days}d",
                'filter_label': filter_label,
                'aggregation': 'union_dedupe_minus_fixed',
                'viewer_role': viewer_role,
            }
        except Exception as e:
            import traceback
            print(f"[Analytics] Error getting summary: {e}")
            traceback.print_exc()
            return {
                'total_commits': 0,
                'total_issues': 0,
                'avg_quality': 0,
                'avg_effort': 0,
                'developers': [],
                'project_summary': [],
                'error': str(e)
            }

    def get_project_wise_summary(self, tl_email: str, days: int = 30,
                                  viewer_role: str = "super_admin") -> List[Dict[str, Any]]:
        """Get project-wise summary for Teams reports with severity breakdown.

        Returns data in format for build_project_wise_report():
        [
            {
                "project_name": "seemyspace",
                "project_id": 1,
                "branches": [
                    {
                        "branch": "develop",
                        "issues": 503,
                        "quality_score": 72.9,
                        "severity_breakdown": {
                            "critical": 18,  # mapped from errors
                            "high": 450,     # mapped from warnings
                            "medium": 35,    # infos above threshold
                            "low": 0         # remaining infos
                        }
                    }
                ],
                "developers": [
                    {
                        "name": "Devendra Singh",
                        "email": "devendra@example.com",
                        "branch": "develop",
                        "commits": 5,
                        "issues": 66,
                        "quality_score": 72.9,
                        "productive_hours": 40,
                        "extra_hours": 8,
                        "critical_issues": [...],
                        "high_issues": [...]
                    }
                ]
            }
        ]
        """
        try:
            # Get base analytics data
            base_summary = self.get_analytics_summary(
                viewer_email=tl_email,
                viewer_role=viewer_role,
                days=days
            )

            projects_data = []
            project_summaries = base_summary.get('project_summary', [])
            developers = base_summary.get('developers', [])

            for proj in project_summaries:
                proj_id = proj.get('project_id', 0)
                proj_name = proj.get('project_name', 'Unknown')

                # Transform branch data with severity mapping
                branches_data = []
                for br in proj.get('branches', []):
                    errors = int(br.get('errors', 0))
                    warnings = int(br.get('warnings', 0))
                    infos = int(br.get('infos', 0))

                    # Map existing categories to severity levels
                    # errors → critical, warnings → high, infos split → medium/low
                    severity_breakdown = {
                        "critical": errors,  # All errors are critical
                        "high": warnings,    # All warnings are high
                        "medium": min(infos, 50),  # First 50 infos are medium
                        "low": max(0, infos - 50)  # Remaining infos are low
                    }

                    branches_data.append({
                        "branch": br.get('branch', 'unknown'),
                        "issues": int(br.get('issues', 0)),
                        "quality_score": br.get('quality_score', 0) or 0,
                        "is_current": br.get('is_current', False),
                        "severity_breakdown": severity_breakdown,
                        "errors": errors,
                        "warnings": warnings,
                        "infos": infos
                    })

                # Find developers assigned to this project
                proj_developers = []
                for dev in developers:
                    dev_projects = dev.get('projects', [])
                    if proj_name in dev_projects or str(proj_id) in str(dev_projects):
                        # Calculate productivity metrics
                        commits = dev.get('commits', 0)
                        issues = dev.get('issues', 0)
                        effort = dev.get('effort_score', 0)

                        # Productive hours = effort hours for normal work
                        # Extra hours = additional time spent fixing bugs (estimated)
                        productive_hours = round(commits * 0.5, 1)  # 30 min per commit
                        extra_hours = round(issues * 0.3, 1)  # 18 min per issue fix

                        # Get issue counts from branch stats
                        critical_count = 0
                        high_count = 0
                        medium_count = 0
                        low_count = 0
                        issue_details = []

                        # Check branch stats for detailed issue info
                        for br_stat in dev.get('branch_stats', []):
                            if br_stat.get('project_name') == proj_name:
                                # Map errors/warnings/infos to severity levels
                                err_count = br_stat.get('errors', 0) or 0
                                warn_count = br_stat.get('warnings', 0) or 0
                                info_count = br_stat.get('infos', 0) or 0

                                critical_count = err_count
                                high_count = warn_count
                                medium_count = min(info_count, 50)  # First 50 infos = medium
                                low_count = max(0, info_count - 50)  # Rest = low

                                # Generate detailed issue information with explanations
                                if err_count > 0:
                                    issue_details.append({
                                        "title": "Critical Code Quality Issues",
                                        "file": br_stat.get('branch', 'unknown'),
                                        "severity": "critical",
                                        "explanation": f"Found {err_count} critical errors that could cause application crashes, security vulnerabilities, or data loss.",
                                        "fix": "Review error logs in dashboard and fix syntax errors, undefined variables, and security issues immediately."
                                    })
                                if warn_count > 0:
                                    issue_details.append({
                                        "title": "High Priority Warnings",
                                        "file": br_stat.get('branch', 'unknown'),
                                        "severity": "high",
                                        "explanation": f"Found {warn_count} warnings indicating code smells, potential bugs, or performance issues.",
                                        "fix": "Refactor code to follow best practices, remove unused imports, and fix logic errors."
                                    })
                                if info_count > 0:
                                    issue_details.append({
                                        "title": "Code Style & Minor Issues",
                                        "file": br_stat.get('branch', 'unknown'),
                                        "severity": "medium" if info_count <= 50 else "low",
                                        "explanation": f"Found {info_count} style issues, formatting inconsistencies, or minor improvements.",
                                        "fix": "Run code formatter (black/isort) and address style guide violations when convenient."
                                    })

                        proj_developers.append({
                            "name": dev.get('name', dev.get('email', 'Unknown')),
                            "email": dev.get('email', ''),
                            "branch": dev.get('current_branch', 'develop'),
                            "commits": commits,
                            "issues": issues,
                            "quality_score": dev.get('quality_score', 0),
                            "productive_hours": productive_hours,
                            "extra_hours": extra_hours,
                            "critical_count": critical_count,
                            "high_count": high_count,
                            "medium_count": medium_count,
                            "low_count": low_count,
                            "issue_details": issue_details[:5]  # Top 5 issue types
                        })

                # Calculate project totals
                total_commits = sum(d.get('commits', 0) for d in proj_developers)
                total_issues = proj.get('deduped_total', 0)

                projects_data.append({
                    "project_name": proj_name,
                    "project_id": proj_id,
                    "branches": branches_data,
                    "developers": proj_developers,
                    "total_commits": total_commits,
                    "total_issues": total_issues
                })

            return projects_data

        except Exception as e:
            import traceback
            print(f"[Analytics] Error getting project-wise summary: {e}")
            traceback.print_exc()
            return []


# Global tracker instance
_tracker: Optional[AnalyticsTracker] = None


def get_tracker() -> AnalyticsTracker:
    """Get or create the global analytics tracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = AnalyticsTracker()
    return _tracker
