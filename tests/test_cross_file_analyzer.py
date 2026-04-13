"""Tests for cross-file analysis: duplicate detection, missing tests, architecture."""

import os
import tempfile
import unittest
from pathlib import Path

from agent.analyzer.cross_file_analyzer import (
    detect_architecture_issues,
    detect_cross_file_duplicates,
    detect_missing_test_files,
)


class TestCrossFileDuplicates(unittest.TestCase):
    """Test deterministic cross-file function duplication detection."""

    def _write(self, tmpdir: str, name: str, content: str) -> str:
        path = os.path.join(tmpdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def test_identical_functions_across_files_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Two files with identical function bodies (> 80 chars normalised)
            body = (
                "def process_data(items):\n"
                "    result = []\n"
                "    for item in items:\n"
                "        if item.get('active'):\n"
                "            transformed = item['value'] * 2 + 10\n"
                "            result.append(transformed)\n"
                "    return result\n"
            )
            f1 = self._write(tmpdir, "module_a.py", body)
            f2 = self._write(tmpdir, "module_b.py", body)

            violations, stats = detect_cross_file_duplicates([f1, f2], "python")
            self.assertTrue(len(violations) >= 1)
            self.assertEqual(violations[0].rule_id, "CROSS001")
            self.assertGreater(stats.duplicated_lines, 0)
            self.assertGreater(stats.percentage, 0)

    def test_unique_functions_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = self._write(tmpdir, "a.py", "def foo():\n    return 1\n")
            f2 = self._write(tmpdir, "b.py", "def bar():\n    return 2\n")
            violations, stats = detect_cross_file_duplicates([f1, f2], "python")
            self.assertEqual(len(violations), 0)
            self.assertEqual(stats.duplicated_lines, 0)

    def test_js_duplicate_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            body = (
                "function processItems(items) {\n"
                "    const result = [];\n"
                "    for (const item of items) {\n"
                "        if (item.active) {\n"
                "            const transformed = item.value * 2 + 10;\n"
                "            result.push(transformed);\n"
                "        }\n"
                "    }\n"
                "    return result;\n"
                "}\n"
            )
            f1 = self._write(tmpdir, "utils.js", body)
            f2 = self._write(tmpdir, "helpers.js", body)
            violations, stats = detect_cross_file_duplicates([f1, f2], "javascript")
            self.assertTrue(len(violations) >= 1)
            self.assertGreater(stats.percentage, 0)


class TestMissingTestFiles(unittest.TestCase):
    """Test missing test file detection."""

    def _write(self, tmpdir: str, name: str, content: str = "") -> str:
        path = os.path.join(tmpdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(content or "# placeholder", encoding="utf-8")
        return path

    def test_missing_test_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = self._write(tmpdir, "service.py")
            violations = detect_missing_test_files([src], tmpdir, "python")
            self.assertTrue(len(violations) >= 1)
            self.assertEqual(violations[0].rule_id, "CROSS002")
            self.assertIn("test_service.py", violations[0].message)

    def test_existing_test_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = self._write(tmpdir, "service.py")
            self._write(tmpdir, "tests/test_service.py")
            violations = detect_missing_test_files([src], tmpdir, "python")
            self.assertEqual(len(violations), 0)

    def test_init_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = self._write(tmpdir, "__init__.py")
            violations = detect_missing_test_files([src], tmpdir, "python")
            self.assertEqual(len(violations), 0)

    def test_js_missing_test_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = self._write(tmpdir, "utils.ts", "export const x = 1;")
            violations = detect_missing_test_files([src], tmpdir, "typescript")
            self.assertTrue(len(violations) >= 1)

    def test_js_existing_spec_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = self._write(tmpdir, "utils.ts", "export const x = 1;")
            self._write(tmpdir, "__tests__/utils.spec.ts")
            violations = detect_missing_test_files([src], tmpdir, "typescript")
            self.assertEqual(len(violations), 0)

    def test_files_in_test_dir_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = self._write(tmpdir, "tests/conftest.py")
            # File inside tests/ dir should be skipped
            violations = detect_missing_test_files([src], tmpdir, "python")
            self.assertEqual(len(violations), 0)


class TestArchitectureIssues(unittest.TestCase):
    """Test architecture / structure detection."""

    def test_missing_gitignore_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # No .gitignore
            violations = detect_architecture_issues(tmpdir, "python", None, [])
            rule_ids = [v.rule_id for v in violations]
            self.assertIn("ARCH001", rule_ids)

    def test_gitignore_present_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            Path(tmpdir, "README.md").write_text("# Project\n", encoding="utf-8")
            violations = detect_architecture_issues(tmpdir, "python", None, [])
            universal_ids = [v.rule_id for v in violations if v.rule_id == "ARCH001"]
            self.assertEqual(len(universal_ids), 0)

    def test_missing_env_example_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, ".gitignore").write_text(".env\n", encoding="utf-8")
            Path(tmpdir, "README.md").write_text("# Project\n", encoding="utf-8")
            violations = detect_architecture_issues(tmpdir, "python", None, [])
            env_violations = [v for v in violations if v.rule_id == "ARCH003"]
            self.assertEqual(len(env_violations), 1)

    def test_large_file_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            large_file = os.path.join(tmpdir, "big.py")
            Path(large_file).write_text("\n".join(["x = 1"] * 350), encoding="utf-8")
            Path(tmpdir, ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            Path(tmpdir, "README.md").write_text("# Project\n", encoding="utf-8")
            violations = detect_architecture_issues(tmpdir, "python", None, [large_file])
            arch5 = [v for v in violations if v.rule_id == "ARCH005"]
            self.assertEqual(len(arch5), 1)
            self.assertIn("350", arch5[0].message)

    def test_framework_structure_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # FastAPI project without app/ directory
            Path(tmpdir, ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            Path(tmpdir, "README.md").write_text("# Project\n", encoding="utf-8")
            Path(tmpdir, "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            violations = detect_architecture_issues(tmpdir, "python", "fastapi", [])
            arch4 = [v for v in violations if v.rule_id == "ARCH004"]
            self.assertTrue(len(arch4) >= 1)


if __name__ == "__main__":
    unittest.main()
