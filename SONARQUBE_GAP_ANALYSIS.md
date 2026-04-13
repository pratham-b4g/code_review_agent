# SonarQube-Level Gap Analysis — UPDATED

> **Last updated after all advanced features were implemented.**
> All previously identified gaps are now **CLOSED**.

---

## Before vs After (Metrics)

| Metric | Before | After |
|---|---|---|
| **Total rules** (Python) | 16 | **33** (23 base + 10 common) |
| **Total rules** (JavaScript) | 14 | **21** |
| **Total rules** (TypeScript) | 20 | **26** |
| **Test cases** | 57 | **145** |
| **SonarQube categories covered** | 4/8 | **8/8** |
| **New modules added** | — | `baseline.py`, `taint_analyzer.py`, `report_generator.py` |
| **New CLI commands** | — | `baseline save`, `report`, `--diff-only`, `--report` |

---

## Full Coverage Map — Current State

| SonarQube Category | Current Coverage | Status |
|---|---|---|
| **Bugs (Correctness)** | `bare_except`, `eval/exec`, `mutable_default_args`, `is_literal_comparison`, `fstring_no_placeholder`, `empty_function_body`, `empty_blocks` | ✅ **COMPLETE** |
| **Security Hotspots** | Hardcoded secrets, JWT in localStorage, CORS wildcard, `.env` file, `shell_injection`, `unsafe_deserialization`, `dangerouslySetInnerHTML`, SQL injection regex, open redirect regex, SSRF regex, **taint analysis** (source→sink data-flow tracing) | ✅ **COMPLETE** |
| **Code Smells / Maintainability** | TODO/FIXME, print statements, `cognitive_complexity`, `cyclomatic_complexity`, `too_many_params`, `duplicate_strings`, `nested_callback_depth`, hardcoded URLs | ✅ **COMPLETE** |
| **Dead Code** | `no_unused_imports` (Python), `no_unused_imports_js` (JS), `unused_variables`, `unreachable_code`, `empty_function_body` | ✅ **COMPLETE** |
| **Duplication** | Cross-file function-level hashing, **cross-method/block AST subtree hashing** (if/for/while/try blocks 5+ lines), JS compound block detection | ✅ **COMPLETE** |
| **Cognitive Complexity** | SonarSource-style scoring with nesting penalties (PY012, threshold: 15), McCabe cyclomatic complexity (PY023, threshold: 10) | ✅ **COMPLETE** |
| **Type Safety** | TS `no_any`, non-null assertion, missing return types, Python `missing_type_hints`, `snake_case_functions` | ✅ **COMPLETE** |
| **Error Handling** | `bare_except`, `empty_except_body`, `async_without_try_catch` | ✅ **COMPLETE** |

---

## What Was Added — Phase by Phase

### Phase 1: Missing AST Checks (Python)
| Rule ID | Check | Category |
|---|---|---|
| PY011 | `mutable_default_args` — `def f(x=[])` classic bug | Bug |
| PY012 | `cognitive_complexity` — SonarSource-style nesting scoring | Maintainability |
| PY013 | `too_many_parameters` — >5 params | Maintainability |
| PY014 | `shell_injection` — `subprocess(shell=True)` | Security |
| PY015 | `unsafe_deserialization` — pickle/yaml/marshal | Security |
| PY016 | `empty_except_body` — `except: pass` | Error Handling |
| PY017 | `unreachable_code` — code after return/raise | Dead Code |
| PY018 | `is_literal_comparison` — `is 1` vs `== 1` | Bug |
| PY019 | `unused_variables` — assigned but never read | Dead Code |
| PY020 | `fstring_no_placeholder` — `f'hello'` with no `{}` | Bug |
| PY021 | `empty_function_body` — only `pass` or `...` | Dead Code |
| PY022 | `duplicate_strings_py` — string repeated 3+ times | Maintainability |
| PY023 | `cyclomatic_complexity` — McCabe decision point counting | Maintainability |

### Phase 1: Missing AST Checks (JavaScript)
| Rule ID | Check | Category |
|---|---|---|
| JS008 | `nested_callback_depth` — callback nesting > 4 | Maintainability |
| JS009 | `too_many_params_js` — >5 params | Maintainability |
| JS010 | `duplicate_strings` — string repeated 3+ times | Maintainability |
| JS011 | `no_dangerously_set_innerhtml` — XSS risk | Security |
| JS012 | `async_without_try_catch` — async without error handling | Error Handling |
| JS013 | `no_unused_imports_js` — unused JS imports | Dead Code |

### Phase 1: Common Security Rules (Regex)
| Rule ID | Check | Category |
|---|---|---|
| COM007 | `no_sql_injection` — raw string formatting in SQL | Security |
| COM008 | `no_open_redirect` — user-controlled redirect URL | Security |
| COM009 | `no_ssrf_patterns` — user-controlled HTTP request target | Security |
| COM010 | `no_empty_blocks` — empty if/for/while blocks | Bug |

### Phase 2: Cross-File Analysis
| Feature | Module | What It Does |
|---|---|---|
| Function-level duplication | `cross_file_analyzer.py` | MD5 hash normalized function bodies across files |
| **Block-level duplication** | `cross_file_analyzer.py` | **NEW** — Hash if/for/while/try blocks (5+ lines) via AST subtree matching |
| Missing test files | `cross_file_analyzer.py` | Flag source files without corresponding test files |
| Architecture checks | `cross_file_analyzer.py` | Missing .gitignore, .env.example, oversized files, framework structure |

### Phase 3: Bug Fixes & Developer Experience
| Feature | Module | What It Does |
|---|---|---|
| `exclude_file_patterns` fix | `rule_engine.py` | Now matches against both full path AND basename |
| Inline suppression | `rule_engine.py` | `# noqa`, `// noqa`, `# cra-ignore` on any line |
| Violation deduplication | `reporter.py` | Same file + line + rule_id collapsed |

### Phase 4: Advanced Features (Previously Listed as "Limitations")

| Previously "Hard" Feature | Implementation | How to Use |
|---|---|---|
| **Diff-only / new-code mode** | `git_utils.py` → `get_changed_lines()` parses `git diff -U0` hunk headers. `rule_engine.py` filters violations to only changed line numbers. | Config: `diff_only: true` or CLI: `--diff-only` |
| **Baseline / ignore-existing** | `baseline.py` — saves/loads violation snapshots per branch in `.cra-baseline/<branch>.json`. `filter_new_violations()` subtracts known issues. | CLI: `python main.py baseline save` then config: `use_baseline: true` |
| **Per-project severity overrides** | `rule_loader.py` — applies `severity_overrides` dict after loading rules. Valid values: `error`, `warning`, `info`. | Config: `severity_overrides: {"PY003": "error", "COM002": "info"}` |
| **Cross-method/class duplication** | `cross_file_analyzer.py` — `_extract_code_blocks_python()` and `_extract_code_blocks_js()` hash compound statement bodies (if/for/while/try, 5+ lines, 60+ chars normalized). | Automatic — runs in cross-file analysis phase |
| **Data-flow / taint analysis** | `taint_analyzer.py` — AST visitor tracks tainted variables from 12+ source patterns (request.args, sys.argv, etc.) through assignments/propagation to 20+ sink functions (cursor.execute, os.system, eval, etc.). Handles Subscript, BinOp, JoinedStr, Call propagation. | Automatic for Python files |
| **Human-readable error messages** | `reporter.py` — `_CATEGORY_WHY` dict adds "Why" explanation per violation category in console. `report_generator.py` — `_human_explanation()` builds What→Why→Fix per violation. | Automatic in console output |
| **Report file generation** | `report_generator.py` — `generate_report_file()` writes structured Markdown with severity sections, code snippets, impact explanations, fix steps, suppression guide. | Auto when violations > 15 (configurable), or CLI: `--report` / `python main.py report` |

---

## Taint Analysis Coverage Detail

| Category | Sources Tracked | Sinks Detected |
|---|---|---|
| SQL Injection | `request.args/form/data/json`, `request.GET/POST` | `cursor.execute`, `execute`, `executemany`, `raw` |
| Command Injection | `sys.argv`, `os.environ`, `input()` | `os.system`, `os.popen`, `subprocess.call/run/Popen` |
| Open Redirect | `request.args/form/headers/cookies` | `redirect()`, `HttpResponseRedirect` |
| SSRF | `request.*`, `request.query_params` | `requests.get/post/put/delete`, `httpx.*`, `urllib.request.urlopen` |
| XSS | `request.*` | `render_template_string`, `Markup` |
| Path Traversal | `request.*`, `sys.argv` | `open()` |
| Code Injection | `request.*`, `sys.argv`, `input()` | `eval()`, `exec()` |

**Propagation**: Tracks through direct assignment, Subscript (`sys.argv[1]`), BinOp (`'SELECT ' + user_input`), f-strings, function call returns, and keyword arguments.

---

## New Config Options

Add to `.code-review-agent.yaml`:

```yaml
# Only flag violations on changed lines (git diff)
diff_only: false

# Suppress known violations from baseline
use_baseline: false

# Override rule severities per project
severity_overrides:
  PY003: error      # Promote print() from warning → error
  COM002: info       # Demote TODO/FIXME from warning → info

# Auto-generate report file when violations exceed this count
report_file_threshold: 15
```

---

## New CLI Commands

```bash
# Review with diff-only mode (only flag new/changed lines)
python main.py review --diff-only

# Review and force-generate a report file
python main.py review --report

# Save current violations as baseline
python main.py baseline save [--dir PATH]

# Generate report on demand
python main.py report [--dir PATH] [--lang python]
```

---

## Test Coverage

| Test File | Tests | Covers |
|---|---|---|
| `test_analyzer.py` | 45 | Python + JS AST checks |
| `test_cross_file_analyzer.py` | 8 | Cross-file duplication, missing tests, architecture |
| `test_detector.py` | 12 | Language/framework detection |
| `test_rule_engine.py` | 52 | Rule loading, regex matching, suppression, deduplication |
| `test_advanced_features.py` | 28 | Diff-only, baseline, severity overrides, taint analysis, report gen, human-readable output, block duplication |
| **Total** | **145** | **All pass** |

---

## What the AI Review Still Handles

The AI prompt (`ai_checks.yaml`) continues to cover things impractical with static analysis:
- **Folder structure validation**
- **Cross-file duplication detection** (semantic, not just hash-based)
- **Missing test files** (context-aware)
- **Architectural suggestions** (design patterns, separation of concerns)

These complement the deterministic rules above.

---

*All gaps identified in the original analysis are now closed. The tool covers all 8 SonarQube quality gate dimensions with 78 deterministic rules, taint analysis, baseline management, diff-only mode, and human-readable reporting.*
