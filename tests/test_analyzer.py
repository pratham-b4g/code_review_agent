"""Tests for Python AST analyzer and JavaScript heuristic analyzer."""

import unittest

from agent.analyzer.javascript_analyzer import JavaScriptAnalyzer
from agent.analyzer.python_analyzer import PythonAnalyzer
from agent.utils.reporter import Severity


def _rule(rule_id: str, ast_check: str, severity: str = "error") -> dict:
    return {
        "id": rule_id,
        "name": rule_id.lower(),
        "severity": severity,
        "message": "Test violation",
        "fix_suggestion": "",
        "category": "test",
        "ast_check": ast_check,
    }


class TestPythonAnalyzer(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = PythonAnalyzer()

    def _check(self, code: str, ast_check: str, severity: str = "error"):
        rule = _rule("TEST", ast_check, severity)
        return self.analyzer.run_ast_check("test.py", code, rule, ast_check)

    # -- bare except -------------------------------------------------------

    def test_bare_except_detected(self) -> None:
        code = "try:\n    pass\nexcept:\n    pass\n"
        violations = self._check(code, "bare_except")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].severity, Severity.ERROR)

    def test_specific_except_not_flagged(self) -> None:
        code = "try:\n    pass\nexcept ValueError:\n    pass\n"
        violations = self._check(code, "bare_except")
        self.assertEqual(len(violations), 0)

    # -- wildcard imports --------------------------------------------------

    def test_wildcard_import_detected(self) -> None:
        code = "from os.path import *\n"
        violations = self._check(code, "wildcard_import")
        self.assertEqual(len(violations), 1)

    def test_normal_import_not_flagged(self) -> None:
        code = "from os.path import join, exists\n"
        violations = self._check(code, "wildcard_import")
        self.assertEqual(len(violations), 0)

    # -- print usage -------------------------------------------------------

    def test_print_detected(self) -> None:
        code = "print('hello')\n"
        violations = self._check(code, "print_usage", "warning")
        self.assertEqual(len(violations), 1)

    def test_logging_not_flagged(self) -> None:
        code = "import logging\nlogging.info('hello')\n"
        violations = self._check(code, "print_usage", "warning")
        self.assertEqual(len(violations), 0)

    # -- eval / exec -------------------------------------------------------

    def test_eval_detected(self) -> None:
        code = "result = eval(user_input)\n"
        violations = self._check(code, "eval_exec_usage")
        self.assertEqual(len(violations), 1)

    def test_exec_detected(self) -> None:
        code = "exec('import os')\n"
        violations = self._check(code, "eval_exec_usage")
        self.assertEqual(len(violations), 1)

    # -- type hints --------------------------------------------------------

    def test_missing_type_hints_detected(self) -> None:
        code = "def greet(name):\n    return 'hi'\n"
        violations = self._check(code, "missing_type_hints", "warning")
        self.assertTrue(len(violations) >= 1)

    def test_fully_typed_function_not_flagged(self) -> None:
        code = "def greet(name: str) -> str:\n    return 'hi'\n"
        violations = self._check(code, "missing_type_hints", "warning")
        self.assertEqual(len(violations), 0)

    # -- snake_case --------------------------------------------------------

    def test_camelcase_function_detected(self) -> None:
        code = "def getUserName():\n    pass\n"
        violations = self._check(code, "snake_case_functions", "warning")
        self.assertEqual(len(violations), 1)

    def test_snake_case_function_not_flagged(self) -> None:
        code = "def get_user_name():\n    pass\n"
        violations = self._check(code, "snake_case_functions", "warning")
        self.assertEqual(len(violations), 0)

    # -- unused imports ----------------------------------------------------

    def test_unused_import_detected(self) -> None:
        code = "import os\n\ndef foo() -> None:\n    pass\n"
        violations = self._check(code, "no_unused_imports", "warning")
        self.assertEqual(len(violations), 1)

    def test_used_import_not_flagged(self) -> None:
        code = "import os\n\ndef foo() -> str:\n    return os.getcwd()\n"
        violations = self._check(code, "no_unused_imports", "warning")
        self.assertEqual(len(violations), 0)

    # -- syntax error handled gracefully -----------------------------------

    def test_syntax_error_returns_empty(self) -> None:
        code = "def broken(:\n    pass\n"
        violations = self._check(code, "bare_except")
        self.assertEqual(violations, [])

    # -- mutable default args (SonarQube) ----------------------------------

    def test_mutable_default_list_detected(self) -> None:
        code = "def append_to(item, target=[]):\n    target.append(item)\n"
        violations = self._check(code, "mutable_default_args")
        self.assertEqual(len(violations), 1)

    def test_none_default_not_flagged(self) -> None:
        code = "def append_to(item, target=None):\n    target = target or []\n"
        violations = self._check(code, "mutable_default_args")
        self.assertEqual(len(violations), 0)

    # -- cognitive complexity (SonarQube) ----------------------------------

    def test_high_complexity_detected(self) -> None:
        # Deeply nested function
        code = (
            "def complex_func(x):\n"
            "    if x > 0:\n"
            "        for i in range(x):\n"
            "            if i > 5:\n"
            "                while True:\n"
            "                    if i == 10:\n"
            "                        break\n"
            "    return x\n"
        )
        rule = _rule("TEST", "cognitive_complexity", "warning")
        rule["threshold"] = 5  # low threshold to trigger
        violations = self.analyzer.run_ast_check("test.py", code, rule, "cognitive_complexity")
        self.assertTrue(len(violations) >= 1)

    def test_simple_function_not_flagged(self) -> None:
        code = "def simple(x: int) -> int:\n    return x + 1\n"
        rule = _rule("TEST", "cognitive_complexity", "warning")
        rule["threshold"] = 15
        violations = self.analyzer.run_ast_check("test.py", code, rule, "cognitive_complexity")
        self.assertEqual(len(violations), 0)

    # -- too many params (SonarQube) ---------------------------------------

    def test_too_many_params_detected(self) -> None:
        code = "def func(a, b, c, d, e, f, g):\n    pass\n"
        rule = _rule("TEST", "too_many_params", "warning")
        rule["threshold"] = 5
        violations = self.analyzer.run_ast_check("test.py", code, rule, "too_many_params")
        self.assertEqual(len(violations), 1)

    def test_self_excluded_from_param_count(self) -> None:
        code = "class C:\n    def method(self, a, b, c):\n        pass\n"
        rule = _rule("TEST", "too_many_params", "warning")
        rule["threshold"] = 5
        violations = self.analyzer.run_ast_check("test.py", code, rule, "too_many_params")
        self.assertEqual(len(violations), 0)

    # -- shell injection (SonarQube) ---------------------------------------

    def test_shell_true_detected(self) -> None:
        code = "import subprocess\nsubprocess.run('ls', shell=True)\n"
        violations = self._check(code, "shell_injection")
        self.assertEqual(len(violations), 1)

    def test_shell_false_not_flagged(self) -> None:
        code = "import subprocess\nsubprocess.run(['ls'], shell=False)\n"
        violations = self._check(code, "shell_injection")
        self.assertEqual(len(violations), 0)

    # -- unsafe deserialization (SonarQube) --------------------------------

    def test_pickle_loads_detected(self) -> None:
        code = "import pickle\ndata = pickle.loads(raw)\n"
        violations = self._check(code, "unsafe_deserialization")
        self.assertEqual(len(violations), 1)

    def test_yaml_load_without_safe_detected(self) -> None:
        code = "import yaml\ndata = yaml.load(text)\n"
        violations = self._check(code, "unsafe_deserialization")
        self.assertTrue(len(violations) >= 1)

    def test_yaml_safe_load_not_flagged(self) -> None:
        code = "import yaml\ndata = yaml.safe_load(text)\n"
        violations = self._check(code, "unsafe_deserialization")
        self.assertEqual(len(violations), 0)

    # -- empty except body (SonarQube) -------------------------------------

    def test_empty_except_pass_detected(self) -> None:
        code = "try:\n    x = 1\nexcept Exception:\n    pass\n"
        violations = self._check(code, "empty_except_body", "warning")
        self.assertEqual(len(violations), 1)

    def test_except_with_logging_not_flagged(self) -> None:
        code = "try:\n    x = 1\nexcept Exception as e:\n    logger.error(e)\n"
        violations = self._check(code, "empty_except_body", "warning")
        self.assertEqual(len(violations), 0)

    # -- unreachable code (SonarQube) --------------------------------------

    def test_unreachable_after_return_detected(self) -> None:
        code = "def f():\n    return 1\n    x = 2\n"
        violations = self._check(code, "unreachable_code", "warning")
        self.assertEqual(len(violations), 1)

    def test_no_unreachable_code(self) -> None:
        code = "def f():\n    x = 2\n    return x\n"
        violations = self._check(code, "unreachable_code", "warning")
        self.assertEqual(len(violations), 0)

    # -- is literal comparison (SonarQube) ---------------------------------

    def test_is_with_int_detected(self) -> None:
        code = "x = 1\nif x is 1:\n    pass\n"
        violations = self._check(code, "is_literal_comparison")
        self.assertEqual(len(violations), 1)

    def test_is_none_not_flagged(self) -> None:
        code = "if x is None:\n    pass\n"
        violations = self._check(code, "is_literal_comparison")
        self.assertEqual(len(violations), 0)

    # -- unused variables (SonarQube) --------------------------------------

    def test_unused_var_detected(self) -> None:
        code = "def f():\n    unused = 42\n    return 1\n"
        violations = self._check(code, "unused_variables", "warning")
        self.assertEqual(len(violations), 1)

    def test_used_var_not_flagged(self) -> None:
        code = "def f():\n    x = 42\n    return x\n"
        violations = self._check(code, "unused_variables", "warning")
        self.assertEqual(len(violations), 0)

    def test_underscore_prefix_not_flagged(self) -> None:
        code = "def f():\n    _unused = 42\n    return 1\n"
        violations = self._check(code, "unused_variables", "warning")
        self.assertEqual(len(violations), 0)

    # -- f-string without placeholder (SonarQube) -------------------------

    def test_fstring_no_placeholder_detected(self) -> None:
        code = "x = f'hello world'\n"
        violations = self._check(code, "fstring_no_placeholder", "warning")
        self.assertEqual(len(violations), 1)

    def test_fstring_with_placeholder_not_flagged(self) -> None:
        code = "name = 'Bob'\nx = f'hello {name}'\n"
        violations = self._check(code, "fstring_no_placeholder", "warning")
        self.assertEqual(len(violations), 0)

    # -- empty function body (SonarQube) -----------------------------------

    def test_empty_function_pass_detected(self) -> None:
        code = "def stub():\n    pass\n"
        violations = self._check(code, "empty_function_body", "warning")
        self.assertEqual(len(violations), 1)

    def test_empty_function_ellipsis_detected(self) -> None:
        code = "def stub():\n    ...\n"
        violations = self._check(code, "empty_function_body", "warning")
        self.assertEqual(len(violations), 1)

    def test_function_with_body_not_flagged(self) -> None:
        code = "def real():\n    return 42\n"
        violations = self._check(code, "empty_function_body", "warning")
        self.assertEqual(len(violations), 0)

    def test_abstractmethod_not_flagged(self) -> None:
        code = "from abc import abstractmethod\nclass C:\n    @abstractmethod\n    def m(self):\n        pass\n"
        violations = self._check(code, "empty_function_body", "warning")
        self.assertEqual(len(violations), 0)

    # -- duplicate strings (Python) ----------------------------------------

    def test_duplicate_strings_py_detected(self) -> None:
        code = (
            "a = 'repeated_string'\n"
            "b = 'repeated_string'\n"
            "c = 'repeated_string'\n"
        )
        rule = _rule("TEST", "duplicate_strings_py", "warning")
        rule["threshold"] = 3
        violations = self.analyzer.run_ast_check("test.py", code, rule, "duplicate_strings_py")
        self.assertEqual(len(violations), 1)

    def test_duplicate_strings_py_unique_not_flagged(self) -> None:
        code = "a = 'unique_one'\nb = 'unique_two'\n"
        rule = _rule("TEST", "duplicate_strings_py", "warning")
        rule["threshold"] = 3
        violations = self.analyzer.run_ast_check("test.py", code, rule, "duplicate_strings_py")
        self.assertEqual(len(violations), 0)

    # -- cyclomatic complexity (McCabe) ------------------------------------

    def test_cyclomatic_high_detected(self) -> None:
        code = (
            "def messy(x, y, z):\n"
            "    if x:\n"
            "        if y:\n"
            "            for i in z:\n"
            "                if i > 0:\n"
            "                    while True:\n"
            "                        if i % 2 == 0:\n"
            "                            break\n"
            "                        elif i % 3 == 0:\n"
            "                            continue\n"
            "                        elif i % 5 == 0:\n"
            "                            pass\n"
            "    if x and y and z:\n"
            "        return 1\n"
            "    return 0\n"
        )
        rule = _rule("TEST", "cyclomatic_complexity", "warning")
        rule["threshold"] = 5
        violations = self.analyzer.run_ast_check("test.py", code, rule, "cyclomatic_complexity")
        self.assertTrue(len(violations) >= 1)

    def test_cyclomatic_low_not_flagged(self) -> None:
        code = "def simple(x):\n    if x:\n        return 1\n    return 0\n"
        rule = _rule("TEST", "cyclomatic_complexity", "warning")
        rule["threshold"] = 10
        violations = self.analyzer.run_ast_check("test.py", code, rule, "cyclomatic_complexity")
        self.assertEqual(len(violations), 0)


class TestJavaScriptAnalyzer(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = JavaScriptAnalyzer()

    def _check(self, code: str, ast_check: str, severity: str = "warning"):
        rule = _rule("JSTEST", ast_check, severity)
        return self.analyzer.run_ast_check("test.tsx", code, rule, ast_check)

    def test_class_component_detected(self) -> None:
        code = "class MyComp extends React.Component {\n  render() { return null; }\n}\n"
        violations = self._check(code, "no_class_components")
        self.assertEqual(len(violations), 1)

    def test_functional_component_not_flagged(self) -> None:
        code = "const MyComp = () => null;\n"
        violations = self._check(code, "no_class_components")
        self.assertEqual(len(violations), 0)

    def test_console_log_detected(self) -> None:
        code = "function foo() { console.log('debug'); }\n"
        violations = self._check(code, "no_console_log")
        self.assertEqual(len(violations), 1)

    def test_console_in_comment_not_flagged(self) -> None:
        code = "// console.log('debug');\n"
        violations = self._check(code, "no_console_log")
        self.assertEqual(len(violations), 0)

    def test_var_detected(self) -> None:
        code = "var x = 1;\n"
        violations = self._check(code, "no_var_declaration")
        self.assertEqual(len(violations), 1)

    def test_jwt_in_localstorage_detected(self) -> None:
        code = "localStorage.setItem('token', jwt);\n"
        violations = self._check(code, "no_jwt_in_localstorage", "error")
        self.assertEqual(len(violations), 1)

    def test_asyncstorage_secret_detected(self) -> None:
        code = "AsyncStorage.setItem('token', value);\n"
        violations = self._check(code, "no_async_storage_secrets", "error")
        self.assertEqual(len(violations), 1)

    def test_any_type_detected(self) -> None:
        code = "const x: any = {};\n"
        violations = self._check(code, "no_any_type")
        self.assertEqual(len(violations), 1)

    def test_raw_anchor_detected(self) -> None:
        code = '<a href="/home">Home</a>\n'
        violations = self._check(code, "no_raw_anchor")
        self.assertEqual(len(violations), 1)

    def test_external_anchor_not_flagged(self) -> None:
        code = '<a href="https://example.com">External</a>\n'
        violations = self._check(code, "no_raw_anchor")
        self.assertEqual(len(violations), 0)

    # -- dangerouslySetInnerHTML (SonarQube) --------------------------------

    def test_dangerously_set_innerhtml_detected(self) -> None:
        code = '<div dangerouslySetInnerHTML={{ __html: content }} />\n'
        violations = self._check(code, "no_dangerously_set_innerhtml", "error")
        self.assertEqual(len(violations), 1)

    def test_no_dangerously_set_innerhtml_clean(self) -> None:
        code = "<div>{content}</div>\n"
        violations = self._check(code, "no_dangerously_set_innerhtml", "error")
        self.assertEqual(len(violations), 0)

    # -- duplicate strings (SonarQube) -------------------------------------

    def test_duplicate_strings_detected(self) -> None:
        code = (
            'const a = "some_long_string";\n'
            'const b = "some_long_string";\n'
            'const c = "some_long_string";\n'
        )
        rule = _rule("JSTEST", "duplicate_strings", "warning")
        rule["threshold"] = 3
        violations = self.analyzer.run_ast_check("test.tsx", code, rule, "duplicate_strings")
        self.assertEqual(len(violations), 1)

    def test_no_duplicate_strings_unique(self) -> None:
        code = 'const a = "unique_value_one";\nconst b = "unique_value_two";\n'
        rule = _rule("JSTEST", "duplicate_strings", "warning")
        rule["threshold"] = 3
        violations = self.analyzer.run_ast_check("test.tsx", code, rule, "duplicate_strings")
        self.assertEqual(len(violations), 0)

    # -- unused imports JS (SonarQube) -------------------------------------

    def test_unused_import_detected(self) -> None:
        code = 'import { useState } from "react";\nconst x = 1;\n'
        violations = self._check(code, "no_unused_imports_js")
        self.assertEqual(len(violations), 1)

    def test_used_import_not_flagged(self) -> None:
        code = 'import { useState } from "react";\nconst [x, setX] = useState(0);\n'
        violations = self._check(code, "no_unused_imports_js")
        self.assertEqual(len(violations), 0)

    def test_default_import_unused(self) -> None:
        code = 'import React from "react";\nconst x = 1;\n'
        violations = self._check(code, "no_unused_imports_js")
        self.assertEqual(len(violations), 1)

    def test_default_import_used(self) -> None:
        code = 'import React from "react";\nconst el = React.createElement("div");\n'
        violations = self._check(code, "no_unused_imports_js")
        self.assertEqual(len(violations), 0)


if __name__ == "__main__":
    unittest.main()
