"""Tests for the RuleEngine, RuleLoader, and rule validator."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent.rules.rule_engine import RuleEngine
from agent.rules.rule_loader import RuleLoader
from agent.rules.rule_validator import validate_rule, validate_rule_file
from agent.utils.reporter import Severity


class TestRuleValidator(unittest.TestCase):
    def test_valid_regex_rule(self) -> None:
        rule = {
            "id": "TEST001",
            "name": "test_rule",
            "severity": "error",
            "type": "regex",
            "pattern": "foo",
            "message": "Found foo",
        }
        valid, errors = validate_rule(rule)
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_missing_required_field(self) -> None:
        rule = {"id": "TEST002", "name": "no_severity", "type": "regex", "message": "x"}
        valid, errors = validate_rule(rule)
        self.assertFalse(valid)
        self.assertTrue(any("severity" in e for e in errors))

    def test_invalid_severity(self) -> None:
        rule = {
            "id": "T003",
            "name": "bad_sev",
            "severity": "critical",
            "type": "regex",
            "pattern": "x",
            "message": "x",
        }
        valid, errors = validate_rule(rule)
        self.assertFalse(valid)
        self.assertTrue(any("severity" in e for e in errors))

    def test_regex_rule_missing_pattern(self) -> None:
        rule = {
            "id": "T004",
            "name": "no_pattern",
            "severity": "warning",
            "type": "regex",
            "message": "x",
        }
        valid, errors = validate_rule(rule)
        self.assertFalse(valid)
        self.assertTrue(any("pattern" in e for e in errors))

    def test_valid_rule_file(self) -> None:
        data = {
            "rules": [
                {
                    "id": "T005",
                    "name": "ok",
                    "severity": "info",
                    "type": "regex",
                    "pattern": "x",
                    "message": "x",
                }
            ]
        }
        valid, errors = validate_rule_file(data)
        self.assertTrue(valid)

    def test_duplicate_rule_ids(self) -> None:
        data = {
            "rules": [
                {"id": "DUP", "name": "a", "severity": "info", "type": "regex", "pattern": "x", "message": "x"},
                {"id": "DUP", "name": "b", "severity": "info", "type": "regex", "pattern": "y", "message": "y"},
            ]
        }
        valid, errors = validate_rule_file(data)
        self.assertFalse(valid)
        self.assertTrue(any("Duplicate" in e for e in errors))


class TestRuleLoader(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        # Create a minimal rule directory structure
        for d in ["common", "python", "javascript"]:
            os.makedirs(os.path.join(self.tmpdir, d), exist_ok=True)

    def _write_rules(self, path: str, rules: list) -> None:
        full_path = os.path.join(self.tmpdir, path)
        Path(full_path).write_text(
            json.dumps({"rules": rules}), encoding="utf-8"
        )

    def _make_rule(self, rule_id: str, name: str = "test", severity: str = "warning") -> dict:
        return {
            "id": rule_id,
            "name": name,
            "severity": severity,
            "type": "regex",
            "pattern": "foo",
            "message": "Found foo",
            "enabled": True,
        }

    def test_loads_common_and_language_rules(self) -> None:
        self._write_rules("common/common_rules.json", [self._make_rule("C001")])
        self._write_rules("python/base_rules.json", [self._make_rule("PY001")])
        loader = RuleLoader(rules_dir=self.tmpdir)
        rules = loader.load_rules("python", None)
        ids = [r["id"] for r in rules]
        self.assertIn("C001", ids)
        self.assertIn("PY001", ids)

    def test_loads_framework_rules(self) -> None:
        self._write_rules("python/base_rules.json", [self._make_rule("PY001")])
        self._write_rules("python/fastapi_rules.json", [self._make_rule("FAPI001")])
        loader = RuleLoader(rules_dir=self.tmpdir)
        rules = loader.load_rules("python", "fastapi")
        ids = [r["id"] for r in rules]
        self.assertIn("FAPI001", ids)

    def test_disabled_rules_not_loaded(self) -> None:
        self._write_rules(
            "python/base_rules.json",
            [
                {**self._make_rule("PY_ENABLED"), "enabled": True},
                {**self._make_rule("PY_DISABLED"), "enabled": False},
            ],
        )
        loader = RuleLoader(rules_dir=self.tmpdir)
        rules = loader.load_rules("python", None)
        ids = [r["id"] for r in rules]
        self.assertIn("PY_ENABLED", ids)
        self.assertNotIn("PY_DISABLED", ids)

    def test_no_duplicate_ids_across_files(self) -> None:
        self._write_rules("common/common_rules.json", [self._make_rule("SHARED001")])
        self._write_rules("python/base_rules.json", [self._make_rule("SHARED001")])
        loader = RuleLoader(rules_dir=self.tmpdir)
        rules = loader.load_rules("python", None)
        ids = [r["id"] for r in rules]
        self.assertEqual(ids.count("SHARED001"), 1)


class TestRuleEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RuleEngine()
        self.tmpdir = tempfile.mkdtemp()

    def _write_file(self, name: str, content: str) -> str:
        path = os.path.join(self.tmpdir, name)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def _regex_rule(self, rule_id: str, pattern: str, severity: str = "error") -> dict:
        return {
            "id": rule_id,
            "name": rule_id.lower(),
            "severity": severity,
            "type": "regex",
            "pattern": pattern,
            "message": f"{rule_id} violation",
            "file_extensions": [],
            "enabled": True,
        }

    def test_regex_violation_found(self) -> None:
        path = self._write_file("test.py", "console.log('debug')\n")
        rules = [self._regex_rule("TEST001", r"console\.log")]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 1)
        self.assertEqual(result.violations[0].severity, Severity.ERROR)

    def test_no_violation_when_pattern_absent(self) -> None:
        path = self._write_file("test.py", "x = 1\n")
        rules = [self._regex_rule("TEST002", r"console\.log")]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 0)

    def test_extension_filter_applied(self) -> None:
        path = self._write_file("test.md", "console.log('debug')\n")
        rules = [
            {**self._regex_rule("JS001", r"console\.log"), "file_extensions": [".js"]}
        ]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 0)

    def test_multiple_violations_per_file(self) -> None:
        path = self._write_file("test.py", "print('a')\nprint('b')\n")
        rules = [self._regex_rule("PY003", r"\bprint\s*\(", "warning")]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 2)

    def test_disabled_rule_skipped(self) -> None:
        path = self._write_file("test.py", "console.log('x')\n")
        rule = {**self._regex_rule("TEST003", r"console\.log"), "enabled": False}
        result = self.engine.review_files([path], [rule])
        self.assertEqual(len(result.violations), 0)

    def test_files_scanned_counter(self) -> None:
        paths = [
            self._write_file("a.py", "x = 1"),
            self._write_file("b.py", "y = 2"),
        ]
        result = self.engine.review_files(paths, [])
        self.assertEqual(result.files_scanned, 2)

    def test_has_blocking_issues(self) -> None:
        path = self._write_file("test.py", "eval('bad')\n")
        rules = [self._regex_rule("SEC001", r"\beval\s*\(", "error")]
        result = self.engine.review_files([path], rules)
        # error-severity violations always block regardless of block_on_warning
        self.assertTrue(result.has_blocking_issues())
        self.assertTrue(result.has_blocking_issues(block_on_warning=False))

    def test_warning_only_not_blocking_by_default(self) -> None:
        path = self._write_file("test.py", "print('x')\n")
        rules = [self._regex_rule("W001", r"\bprint\s*\(", "warning")]
        result = self.engine.review_files([path], rules)
        self.assertFalse(result.has_blocking_issues())
        self.assertTrue(result.has_blocking_issues(block_on_warning=True))

    def test_exclude_path_skipped(self) -> None:
        node_dir = os.path.join(self.tmpdir, "node_modules")
        os.makedirs(node_dir, exist_ok=True)
        path = os.path.join(node_dir, "lib.js")
        Path(path).write_text("eval('x')\n", encoding="utf-8")
        rules = [self._regex_rule("SEC001", r"\beval\s*\(")]
        result = self.engine.review_files(
            [path], rules, exclude_paths=["node_modules"]
        )
        self.assertEqual(len(result.violations), 0)

    # -- exclude_file_patterns with full paths (bug fix) ------------------

    def test_exclude_file_patterns_matches_basename(self) -> None:
        """exclude_file_patterns like 'test_*.py' must match even on full paths."""
        path = self._write_file("test_something.py", "eval('x')\n")
        rule = {
            **self._regex_rule("SEC001", r"\beval\s*\("),
            "exclude_file_patterns": ["test_*.py"],
        }
        result = self.engine.review_files([path], [rule])
        self.assertEqual(len(result.violations), 0)

    def test_exclude_file_patterns_no_false_exclude(self) -> None:
        """Non-matching files should still be checked."""
        path = self._write_file("service.py", "eval('x')\n")
        rule = {
            **self._regex_rule("SEC001", r"\beval\s*\("),
            "exclude_file_patterns": ["test_*.py"],
        }
        result = self.engine.review_files([path], [rule])
        self.assertEqual(len(result.violations), 1)

    # -- inline suppression (# noqa / // noqa / # cra-ignore) -------------

    def test_noqa_suppresses_regex_violation(self) -> None:
        path = self._write_file("test.py", "eval('ok')  # noqa\n")
        rules = [self._regex_rule("SEC001", r"\beval\s*\(")]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 0)

    def test_cra_ignore_suppresses_violation(self) -> None:
        path = self._write_file("test.py", "eval('ok')  # cra-ignore\n")
        rules = [self._regex_rule("SEC001", r"\beval\s*\(")]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 0)

    def test_js_noqa_suppresses_violation(self) -> None:
        path = self._write_file("test.js", "console.log('x'); // noqa\n")
        rules = [self._regex_rule("JS001", r"console\.log")]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 0)

    def test_line_without_noqa_still_flagged(self) -> None:
        path = self._write_file("test.py", "eval('bad')\n")
        rules = [self._regex_rule("SEC001", r"\beval\s*\(")]
        result = self.engine.review_files([path], rules)
        self.assertEqual(len(result.violations), 1)

    # -- deduplication -----------------------------------------------------

    def test_deduplicate_removes_exact_dupes(self) -> None:
        from agent.utils.reporter import ReviewResult, Violation, Severity
        result = ReviewResult()
        v = Violation(
            rule_id="X", rule_name="x", severity=Severity.ERROR,
            file_path="a.py", line_number=1, message="msg",
        )
        result.violations = [v, v, v]
        result.deduplicate()
        self.assertEqual(len(result.violations), 1)

    def test_deduplicate_keeps_different_rules(self) -> None:
        from agent.utils.reporter import ReviewResult, Violation, Severity
        result = ReviewResult()
        result.violations = [
            Violation(rule_id="A", rule_name="a", severity=Severity.ERROR, file_path="x.py", line_number=1, message="m1"),
            Violation(rule_id="B", rule_name="b", severity=Severity.ERROR, file_path="x.py", line_number=1, message="m2"),
        ]
        result.deduplicate()
        self.assertEqual(len(result.violations), 2)


if __name__ == "__main__":
    unittest.main()
