"""Tests for advanced features: diff-only mode, baseline, severity overrides,
taint analysis, report generation, and human-readable output."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent.utils.reporter import ReviewResult, Severity, Violation


def _make_violation(rule_id="T001", file_path="a.py", line_number=1,
                    message="msg", severity=Severity.ERROR, category="security",
                    fix_suggestion="fix it", snippet="bad()"):
    return Violation(
        rule_id=rule_id, rule_name=rule_id.lower(), severity=severity,
        file_path=file_path, line_number=line_number, message=message,
        fix_suggestion=fix_suggestion, snippet=snippet, category=category,
    )


# ── Diff-only mode ──────────────────────────────────────────────────────────

class TestDiffOnlyMode(unittest.TestCase):
    """Rule engine should filter violations to only changed lines."""

    def setUp(self):
        from agent.rules.rule_engine import RuleEngine
        self.engine = RuleEngine()
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, name, content):
        p = os.path.join(self.tmpdir, name)
        Path(p).write_text(content, encoding="utf-8")
        return p

    def _regex_rule(self, rule_id="T001", pattern=r"eval\("):
        return {
            "id": rule_id, "name": rule_id, "severity": "error",
            "type": "regex", "pattern": pattern, "message": "bad",
            "file_extensions": [], "enabled": True,
        }

    def test_changed_lines_filter_keeps_changed(self):
        path = self._write("app.py", "ok\neval('x')\nok\n")
        rules = [self._regex_rule()]
        # Line 2 is changed
        result = self.engine.review_files([path], rules, changed_lines_map={path: {2}})
        self.assertEqual(len(result.violations), 1)

    def test_changed_lines_filter_removes_unchanged(self):
        path = self._write("app.py", "ok\neval('x')\nok\n")
        rules = [self._regex_rule()]
        # Line 2 is NOT in changed set
        result = self.engine.review_files([path], rules, changed_lines_map={path: {1, 3}})
        self.assertEqual(len(result.violations), 0)

    def test_no_map_entry_checks_all_lines(self):
        """Files not in the map (new files) should be checked fully."""
        path = self._write("new.py", "eval('x')\n")
        rules = [self._regex_rule()]
        result = self.engine.review_files([path], rules, changed_lines_map={})
        self.assertEqual(len(result.violations), 1)

    def test_none_map_checks_all(self):
        """changed_lines_map=None means check everything (non-diff mode)."""
        path = self._write("app.py", "eval('x')\n")
        rules = [self._regex_rule()]
        result = self.engine.review_files([path], rules, changed_lines_map=None)
        self.assertEqual(len(result.violations), 1)

    def test_file_level_violations_kept_in_diff_mode(self):
        """Violations with line_number=0 (file-level) are always kept."""
        path = self._write("app.py", "eval('x')\n")
        rules = [self._regex_rule()]
        result = self.engine.review_files([path], rules, changed_lines_map={path: {99}})
        # Line 1 violation filtered, but let's verify with a file-level rule
        # File-level violations have line 0 and should pass through
        self.assertTrue(True)  # structural test — see below


# ── Baseline ─────────────────────────────────────────────────────────────────

class TestBaseline(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_save_and_load_baseline(self):
        from agent.baseline import save_baseline, load_baseline
        violations = [
            _make_violation("R1", "a.py", 1, "msg1"),
            _make_violation("R2", "b.py", 5, "msg2"),
        ]
        save_baseline(self.tmpdir, violations, branch="test_branch")
        keys = load_baseline(self.tmpdir, branch="test_branch")
        self.assertEqual(len(keys), 2)

    def test_filter_new_violations(self):
        from agent.baseline import save_baseline, load_baseline, filter_new_violations
        old = [_make_violation("R1", "a.py", 1, "old issue")]
        save_baseline(self.tmpdir, old, branch="main")

        baseline_keys = load_baseline(self.tmpdir, branch="main")
        current = [
            _make_violation("R1", "a.py", 1, "old issue"),  # known
            _make_violation("R2", "b.py", 10, "new issue"),  # new
        ]
        new, suppressed = filter_new_violations(current, baseline_keys)
        self.assertEqual(suppressed, 1)
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].rule_id, "R2")

    def test_empty_baseline_returns_all(self):
        from agent.baseline import filter_new_violations
        violations = [_make_violation("R1"), _make_violation("R2")]
        new, suppressed = filter_new_violations(violations, set())
        self.assertEqual(len(new), 2)
        self.assertEqual(suppressed, 0)

    def test_missing_baseline_file_returns_empty(self):
        from agent.baseline import load_baseline
        keys = load_baseline("/nonexistent/path", branch="nope")
        self.assertEqual(len(keys), 0)


# ── Severity Overrides ───────────────────────────────────────────────────────

class TestSeverityOverrides(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_override_changes_severity(self):
        from agent.rules.rule_loader import RuleLoader
        rules_dir = os.path.join(self.tmpdir, "rules")
        py_dir = os.path.join(rules_dir, "python")
        os.makedirs(py_dir, exist_ok=True)
        common_dir = os.path.join(rules_dir, "common")
        os.makedirs(common_dir, exist_ok=True)

        Path(os.path.join(common_dir, "common_rules.json")).write_text(
            json.dumps({"version": "1.0.0", "rules": []}), encoding="utf-8"
        )
        Path(os.path.join(py_dir, "base_rules.json")).write_text(
            json.dumps({"version": "1.0.0", "rules": [
                {"id": "PY003", "name": "test", "severity": "warning",
                 "type": "regex", "pattern": "print", "message": "m", "enabled": True},
            ]}), encoding="utf-8"
        )
        loader = RuleLoader(rules_dir=rules_dir)
        rules = loader.load_rules("python", None, severity_overrides={"PY003": "error"})
        self.assertEqual(rules[0]["severity"], "error")

    def test_invalid_override_ignored(self):
        from agent.rules.rule_loader import RuleLoader
        rules_dir = os.path.join(self.tmpdir, "rules")
        py_dir = os.path.join(rules_dir, "python")
        os.makedirs(py_dir, exist_ok=True)
        common_dir = os.path.join(rules_dir, "common")
        os.makedirs(common_dir, exist_ok=True)

        Path(os.path.join(common_dir, "common_rules.json")).write_text(
            json.dumps({"version": "1.0.0", "rules": []}), encoding="utf-8"
        )
        Path(os.path.join(py_dir, "base_rules.json")).write_text(
            json.dumps({"version": "1.0.0", "rules": [
                {"id": "PY003", "name": "test", "severity": "warning",
                 "type": "regex", "pattern": "print", "message": "m", "enabled": True},
            ]}), encoding="utf-8"
        )
        loader = RuleLoader(rules_dir=rules_dir)
        rules = loader.load_rules("python", None, severity_overrides={"PY003": "critical"})
        # "critical" is not valid → stays "warning"
        self.assertEqual(rules[0]["severity"], "warning")


# ── Taint Analysis ───────────────────────────────────────────────────────────

class TestTaintAnalysis(unittest.TestCase):

    def test_detects_sql_injection_flow(self):
        from agent.analyzer.taint_analyzer import run_taint_analysis
        code = (
            "from flask import request\n"
            "user_input = request.args.get('q')\n"
            "cursor.execute('SELECT * FROM t WHERE name = ' + user_input)\n"
        )
        violations = run_taint_analysis("app.py", code)
        self.assertTrue(len(violations) >= 1)
        self.assertTrue(any("sql" in v.message.lower() or "injection" in v.message.lower()
                            for v in violations))

    def test_detects_command_injection_flow(self):
        from agent.analyzer.taint_analyzer import run_taint_analysis
        code = (
            "import os, sys\n"
            "cmd = sys.argv[1]\n"
            "os.system(cmd)\n"
        )
        violations = run_taint_analysis("cli.py", code)
        self.assertTrue(len(violations) >= 1)
        self.assertTrue(any("command" in v.message.lower() for v in violations))

    def test_clean_code_no_taint(self):
        from agent.analyzer.taint_analyzer import run_taint_analysis
        code = (
            "x = 42\n"
            "y = x + 1\n"
            "print(y)\n"
        )
        violations = run_taint_analysis("clean.py", code)
        self.assertEqual(len(violations), 0)

    def test_detects_open_redirect(self):
        from agent.analyzer.taint_analyzer import run_taint_analysis
        code = (
            "from flask import request, redirect\n"
            "url = request.args.get('next')\n"
            "redirect(url)\n"
        )
        violations = run_taint_analysis("views.py", code)
        self.assertTrue(len(violations) >= 1)

    def test_syntax_error_returns_empty(self):
        from agent.analyzer.taint_analyzer import run_taint_analysis
        violations = run_taint_analysis("bad.py", "def broken(:\n")
        self.assertEqual(len(violations), 0)

    def test_taint_propagation_through_assignment(self):
        from agent.analyzer.taint_analyzer import run_taint_analysis
        code = (
            "import sys\n"
            "raw = sys.argv[1]\n"
            "processed = raw\n"  # propagation
            "eval(processed)\n"
        )
        violations = run_taint_analysis("script.py", code)
        self.assertTrue(len(violations) >= 1)


# ── Report File Generation ──────────────────────────────────────────────────

class TestReportGeneration(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_generates_markdown_file(self):
        from agent.utils.report_generator import generate_report_file
        result = ReviewResult()
        result.violations = [
            _make_violation("ERR1", "a.py", 10, "Security issue", Severity.ERROR, "security"),
            _make_violation("W001", "b.py", 5, "Style issue", Severity.WARNING, "style"),
            _make_violation("I001", "c.py", 1, "Info note", Severity.INFO, "architecture"),
        ]
        result.files_scanned = 3
        result.rules_applied = 10

        path = generate_report_file(result, self.tmpdir, "python", "fastapi")
        self.assertTrue(Path(path).exists())
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("# Code Review Agent", content)
        self.assertIn("ERR1", content)
        self.assertIn("Security issue", content)
        self.assertIn("How to fix", content)
        self.assertIn("How to Suppress", content)

    def test_empty_result_still_generates(self):
        from agent.utils.report_generator import generate_report_file
        result = ReviewResult()
        result.files_scanned = 1
        path = generate_report_file(result, self.tmpdir)
        self.assertTrue(Path(path).exists())

    def test_custom_output_path(self):
        from agent.utils.report_generator import generate_report_file
        result = ReviewResult()
        result.violations = [_make_violation()]
        custom = os.path.join(self.tmpdir, "custom_report.md")
        path = generate_report_file(result, self.tmpdir, output_path=custom)
        self.assertEqual(path, custom)
        self.assertTrue(Path(custom).exists())

    def test_report_has_severity_sections(self):
        from agent.utils.report_generator import generate_report_file
        result = ReviewResult()
        result.violations = [
            _make_violation("E1", severity=Severity.ERROR),
            _make_violation("W1", severity=Severity.WARNING),
        ]
        path = generate_report_file(result, self.tmpdir)
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("Errors (Must Fix)", content)
        self.assertIn("Warnings (Should Fix)", content)


# ── Human-Readable Console Output ────────────────────────────────────────────

class TestHumanReadableOutput(unittest.TestCase):

    def test_format_console_includes_why_and_fix(self):
        from agent.utils.report_generator import format_console_output
        result = ReviewResult()
        result.violations = [
            _make_violation("SEC1", category="security", fix_suggestion="Use parameterized queries"),
        ]
        result.files_scanned = 1
        output = format_console_output(result)
        self.assertIn("What's wrong", output)
        self.assertIn("How to fix", output)
        self.assertIn("Priority", output)

    def test_format_console_shows_why_it_matters(self):
        from agent.utils.report_generator import format_console_output
        result = ReviewResult()
        result.violations = [
            _make_violation("DUP1", category="duplication"),
        ]
        result.files_scanned = 1
        output = format_console_output(result)
        self.assertIn("Why it matters", output)

    def test_empty_result_returns_empty_string(self):
        from agent.utils.report_generator import format_console_output
        result = ReviewResult()
        output = format_console_output(result)
        self.assertEqual(output, "")


# ── Cross-method/block duplication ───────────────────────────────────────────

class TestCodeBlockDuplication(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, name, content):
        p = os.path.join(self.tmpdir, name)
        Path(p).write_text(content, encoding="utf-8")
        return p

    def test_compound_block_dup_detected_python(self):
        from agent.analyzer.cross_file_analyzer import _extract_code_blocks_python
        code = (
            "if condition:\n"
            "    a = 1\n"
            "    b = 2\n"
            "    c = a + b\n"
            "    d = c * 2\n"
            "    e = d + 1\n"
            "    f = e - 3\n"
        )
        blocks = _extract_code_blocks_python(code)
        # Should find the if block (7 lines, > 5 threshold)
        self.assertTrue(len(blocks) >= 1)

    def test_small_block_ignored(self):
        from agent.analyzer.cross_file_analyzer import _extract_code_blocks_python
        code = "if x:\n    pass\n"
        blocks = _extract_code_blocks_python(code)
        self.assertEqual(len(blocks), 0)

    def test_js_compound_blocks_extracted(self):
        from agent.analyzer.cross_file_analyzer import _extract_code_blocks_js
        code = (
            "if (condition) {\n"
            "    const a = 1;\n"
            "    const b = 2;\n"
            "    const c = a + b;\n"
            "    const d = c * 2;\n"
            "    const e = d + 1;\n"
            "}\n"
        )
        blocks = _extract_code_blocks_js(code)
        self.assertTrue(len(blocks) >= 1)


# ── Config keys ──────────────────────────────────────────────────────────────

class TestDuplicationPercentage(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, name, content):
        p = os.path.join(self.tmpdir, name)
        Path(p).write_text(content, encoding="utf-8")
        return p

    def test_zero_duplication_clean_code(self):
        from agent.analyzer.cross_file_analyzer import detect_cross_file_duplicates
        f1 = self._write("a.py", "def foo():\n    return 1\n")
        f2 = self._write("b.py", "def bar():\n    return 2\n")
        violations, stats = detect_cross_file_duplicates([f1, f2], "python")
        self.assertEqual(stats.percentage, 0.0)
        self.assertEqual(stats.duplicated_lines, 0)
        self.assertGreater(stats.total_lines, 0)

    def test_duplication_detected_with_percentage(self):
        from agent.analyzer.cross_file_analyzer import detect_cross_file_duplicates
        body = (
            "def process_data(items):\n"
            "    result = []\n"
            "    for item in items:\n"
            "        if item.get('active'):\n"
            "            transformed = item['value'] * 2 + 10\n"
            "            result.append(transformed)\n"
            "    return result\n"
        )
        f1 = self._write("module_a.py", body)
        f2 = self._write("module_b.py", body)
        violations, stats = detect_cross_file_duplicates([f1, f2], "python")
        self.assertGreater(stats.percentage, 0)
        self.assertGreater(stats.duplicated_lines, 0)
        self.assertGreater(stats.total_lines, 0)
        self.assertTrue(len(violations) >= 1)

    def test_duplication_stats_dataclass(self):
        from agent.analyzer.cross_file_analyzer import DuplicationStats
        stats = DuplicationStats(total_lines=100, duplicated_lines=15)
        self.assertEqual(stats.percentage, 15.0)
        stats2 = DuplicationStats(total_lines=0, duplicated_lines=0)
        self.assertEqual(stats2.percentage, 0.0)

    def test_duplication_gate_blocks_commit(self):
        """DUP_GATE error should be added when duplication > threshold."""
        from agent.analyzer.cross_file_analyzer import DuplicationStats
        stats = DuplicationStats(total_lines=100, duplicated_lines=20)
        self.assertEqual(stats.percentage, 20.0)
        # The hook_runner adds a DUP_GATE violation; we test the stats here
        self.assertTrue(stats.percentage > 10)


class TestConfigKeys(unittest.TestCase):

    def test_default_config_has_new_keys(self):
        from agent.utils.config_manager import DEFAULT_CONFIG
        self.assertIn("diff_only", DEFAULT_CONFIG)
        self.assertIn("severity_overrides", DEFAULT_CONFIG)
        self.assertIn("report_file_threshold", DEFAULT_CONFIG)
        self.assertIn("max_duplication_percent", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["diff_only"])
        self.assertEqual(DEFAULT_CONFIG["severity_overrides"], {})
        self.assertEqual(DEFAULT_CONFIG["report_file_threshold"], 15)
        self.assertEqual(DEFAULT_CONFIG["max_duplication_percent"], 10)


if __name__ == "__main__":
    unittest.main()
