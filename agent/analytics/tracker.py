"""Analytics tracker for monitoring developer activity."""
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

    def get_analytics_summary(self, project_id: Optional[int] = None,
                             user_email: Optional[str] = None,
                             days: int = 7) -> Dict[str, Any]:
        """Get analytics summary for the specified period."""
        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        try:
            data = self.db.get_analytics(
                user_email=user_email,
                project_id=project_id,
                start_date=start_date,
                end_date=end_date
            )

            if not data:
                return {
                    'total_commits': 0,
                    'total_issues': 0,
                    'avg_quality': 0,
                    'avg_effort': 0,
                    'developers': []
                }

            # Aggregate stats
            total_commits = sum(d['commits_count'] for d in data)
            total_issues = sum(d['issues_found'] for d in data)

            quality_scores = [d['code_quality_score'] for d in data if d['code_quality_score']]
            effort_scores = [d['effort_score'] for d in data if d['effort_score']]

            avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0
            avg_effort = sum(effort_scores) / len(effort_scores) if effort_scores else 0

            # Group by user
            by_user = {}
            for d in data:
                email = d['user_email']
                if email not in by_user:
                    by_user[email] = {
                        'name': d['user_name'],
                        'email': email,
                        'commits': 0,
                        'issues': 0,
                        'quality_scores': [],
                        'effort_scores': []
                    }
                by_user[email]['commits'] += d['commits_count']
                by_user[email]['issues'] += d['issues_found']
                if d['code_quality_score']:
                    by_user[email]['quality_scores'].append(d['code_quality_score'])
                if d['effort_score']:
                    by_user[email]['effort_scores'].append(d['effort_score'])

            developers = []
            for user_data in by_user.values():
                quality_list = user_data['quality_scores']
                effort_list = user_data['effort_scores']
                developers.append({
                    'name': user_data['name'],
                    'email': user_data['email'],
                    'commits': user_data['commits'],
                    'issues': user_data['issues'],
                    'quality_score': round(sum(quality_list) / len(quality_list), 1) if quality_list else 0,
                    'effort_score': round(sum(effort_list) / len(effort_list), 1) if effort_list else 0
                })

            return {
                'total_commits': total_commits,
                'total_issues': total_issues,
                'avg_quality': round(avg_quality, 1),
                'avg_effort': round(avg_effort, 1),
                'developers': developers,
                'period': f"{start_date} to {end_date}"
            }
        except Exception as e:
            print(f"[Analytics] Error getting summary: {e}")
            return {
                'total_commits': 0,
                'total_issues': 0,
                'avg_quality': 0,
                'avg_effort': 0,
                'developers': [],
                'error': str(e)
            }


# Global instance
_tracker: Optional[AnalyticsTracker] = None


def get_tracker() -> AnalyticsTracker:
    """Get or create the global analytics tracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = AnalyticsTracker()
    return _tracker
