# Code Review Agent

An intelligent git pre-commit gate that automatically reviews your staged code before every commit. It runs linting, rule-based checks, and an AI deep review (powered by Groq) — all in one command.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Installation](#installation)
3. [Uninstallation](#uninstallation)
4. [Commands](#commands)
5. [Rule Files (JSON)](#rule-files-json)
6. [AI Checks (YAML)](#ai-checks-yaml)
7. [AI Response Output](#ai-response-output)
8. [Supported Languages & Frameworks](#supported-languages--frameworks)

---

## How It Works

```
git commit -m "your message"
        │
        ▼
┌─────────────────────────────────────────────────┐
│              Pre-Commit Hook (auto)              │
│                                                 │
│  1. Detect language & framework                 │
│  2. Load matching JSON rule files               │
│  3. Run linting (ruff / eslint)                 │
│  4. Run rule engine on staged files             │
│  5. Run AI deep review via Groq                 │
└─────────────────────────────────────────────────┘
        │
        ▼
  Issues found?
  ├── YES → commit is BLOCKED, errors shown in terminal
  └── NO  → commit goes through ✅
```

### Step-by-step flow

| Step | What happens | File responsible |
|------|-------------|-----------------|
| 1 | Detect language from `package.json`, `requirements.txt`, file extensions | `agent/detector/language_detector.py` |
| 2 | Detect framework from dependencies (`react`, `fastapi`, `express`, etc.) | `agent/detector/framework_detector.py` |
| 3 | Load `common_rules.json` + language base rules + framework rules | `agent/rules/rule_loader.py` |
| 4 | Run linter (ruff for Python, ESLint for JS/TS) | `agent/linter/lint_runner.py` |
| 5 | Apply each rule (regex / AST pattern matching) on every staged file | `agent/rules/rule_engine.py` |
| 6 | Send files + folder structure to Groq AI with checks from `ai_checks.yaml` | `agent/ai/ai_reviewer.py` |
| 7 | Display results in terminal, block commit if high-severity issues found | `agent/utils/reporter.py` |

---

## Installation

### Requirements
- Python 3.9 or higher
- Git

### 1. Install the package

```bash
pip install git+https://github.com/pratham-b4g/code_review_agent.git
```

This installs:
- All dependencies (`groq`, `pyyaml`, `ruff`)
- The `cra` command globally

### 2. Install the git hook into your project

Navigate to your project folder and run:

```bash
cd path/to/your/project
cra install
```

This will:
- Write a `pre-commit` hook into your project's `.git/hooks/`
- Ask you to enter your **Groq API key** (free at https://console.groq.com)
- Save the key permanently as a system environment variable

```
[OK] Pre-commit hook installed at C:\...\project\.git\hooks\pre-commit

[SETUP] AI Review requires a Groq API key (free at https://console.groq.com)
        Enter your GROQ_API_KEY: gsk_xxxxxxxxxxxx

[OK] GROQ_API_KEY saved to Windows environment variables.
```

> After install, every `git commit` in that project will automatically trigger the full review.

---

## Uninstallation

### Remove the hook + API key

```bash
cra uninstall
```

This will:
- Remove the pre-commit hook from your project's `.git/hooks/`
- Ask if you want to remove `GROQ_API_KEY` from your system environment
- Show you the final pip command to remove the package

```
[OK] Pre-commit hook removed from ...\.git\hooks\pre-commit

Do you also want to remove the GROQ_API_KEY from your system? [y/N] y
[OK] GROQ_API_KEY removed from Windows environment variables.

[INFO] To fully remove the package run:  pip uninstall code-review-agent
```

### Remove the package

```bash
pip uninstall code-review-agent
```

---

## Commands

All commands use the `cra` CLI after pip install, or `python main.py` for local development.

### `cra install`

Install the pre-commit hook into the current git repository.

```bash
cra install                          # install in current directory
cra install --repo path/to/project   # install in a specific project
cra install --force                  # overwrite existing hook without prompting
```

---

### `cra uninstall`

Remove the pre-commit hook and optionally clean up the API key.

```bash
cra uninstall                        # uninstall from current directory
cra uninstall --repo path/to/project # uninstall from a specific project
```

---

### `cra review`

Manually run a review. Useful for reviewing without committing.

```bash
# Review staged files only
cra review --staged

# Review staged files with AI
cra review --staged --ai

# Review staged files with AI, skip linting
cra review --staged --ai --skip-lint

# Review a specific project folder
cra review --dir path/to/project

# Review specific files
cra review src/routes/auth.ts src/controllers/user.ts

# Override detected language/framework
cra review --lang typescript --framework express

# Review with AI on a specific project
cra review --ai --skip-lint --staged --dir path/to/project
```

| Flag | Description |
|------|-------------|
| `--staged` | Review only git-staged files |
| `--ai` | Run AI deep review (Groq) after rule checks |
| `--skip-lint` | Skip linting step, go straight to rules + AI |
| `--dir PATH` | Set the project root directory |
| `--lang LANG` | Override language detection (`python`, `javascript`, `typescript`) |
| `--framework FW` | Override framework detection (`react`, `nextjs`, `fastapi`, `express`, etc.) |

---

### `cra rules`

List all rules that will be applied for a given language/framework.

```bash
cra rules --lang javascript --framework express
cra rules --lang python --framework fastapi
cra rules --lang typescript --framework react
```

Example output:
```
Loaded 42 rules for language='javascript' framework='express'

  [ERROR  ] COM001       no_hardcoded_secrets
  [WARNING] COM002       no_todo_fixme_in_push
  [ERROR  ] COM003       no_debug_breakpoints
  [ERROR  ] NODE001      no_console_in_production
  [WARNING] NODE002      route_paths_lowercase
  ...
```

---

## Rule Files (JSON)

Rules are stored in `agent/rules_data/` and loaded automatically based on detected language and framework.

### File structure

```
agent/rules_data/
├── common/
│   └── common_rules.json          ← applied to ALL projects always
├── python/
│   ├── base_rules.json            ← all Python projects
│   ├── fastapi_rules.json         ← FastAPI projects only
│   └── django_rules.json          ← Django projects only
├── javascript/
│   ├── base_rules.json            ← all JS projects
│   ├── nodejs_express_rules.json  ← Express projects only
│   ├── react_rules.json           ← React projects only
│   ├── nextjs_rules.json          ← Next.js projects only
│   └── react_native_rules.json    ← React Native projects only
└── typescript/
    └── base_rules.json            ← all TypeScript projects
```

### Load order

For an Express/TypeScript project, rules are loaded in this order:

```
1. common/common_rules.json
2. javascript/base_rules.json
3. typescript/base_rules.json
4. javascript/nodejs_express_rules.json
```

Duplicate rule IDs are skipped — the first loaded version wins.

### Rule structure

Each JSON file has this format:

```json
{
  "version": "1.0.0",
  "description": "Description of this rule set",
  "rules": [
    {
      "id": "COM001",
      "name": "no_hardcoded_secrets",
      "description": "Detect hardcoded API keys or passwords in source files",
      "severity": "error",
      "category": "security",
      "type": "regex",
      "pattern": "(?i)(api[_-]?key|secret)\\s*[:=]\\s*['\"][A-Za-z0-9]{8,}['\"]",
      "message": "Hardcoded secret detected. Never commit credentials to version control.",
      "fix_suggestion": "Move this value to an environment variable (os.getenv / process.env).",
      "file_extensions": [".js", ".ts", ".py"],
      "exclude_file_patterns": ["*.example", "*.md"],
      "enabled": true
    }
  ]
}
```

### Rule fields explained

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique rule ID (e.g. `COM001`, `JS005`) |
| `name` | Yes | Short snake_case name |
| `description` | Yes | What the rule checks |
| `severity` | Yes | `error` (blocks commit) or `warning` (warns only) |
| `category` | Yes | `security`, `style`, `correctness`, `logging`, `debug`, etc. |
| `type` | Yes | `regex` (pattern match) or `ast` (code structure check) |
| `pattern` | Yes | Regex pattern to search for in file content |
| `message` | Yes | Message shown in terminal when rule is violated |
| `fix_suggestion` | Yes | How to fix the violation |
| `file_extensions` | No | List of extensions to check (empty = all files) |
| `exclude_file_patterns` | No | File patterns to skip (e.g. `*.test.*`, `*.md`) |
| `enabled` | No | Set `false` to disable a rule without deleting it |

### Rule types

**`regex`** — searches for a pattern anywhere in the file:
```json
{
  "type": "regex",
  "pattern": "\\bconsole\\.(log|warn|error)\\s*\\("
}
```

**`ast`** — uses AST analysis for smarter detection (with regex fallback):
```json
{
  "type": "ast",
  "ast_check": "no_console_log",
  "fallback_pattern": "console\\.(log|warn|error)\\s*\\("
}
```

Available `ast_check` values:
- `no_console_log` — detects console statements
- `no_var_declaration` — detects `var` usage
- `missing_error_handling` — detects async functions without try/catch

**`filename`** — checks the file path/name itself:
```json
{
  "type": "filename",
  "pattern": "(^|/)\\.env$",
  "expect_match": false
}
```

### Adding a new rule

1. Open the relevant JSON file (e.g. `agent/rules_data/javascript/base_rules.json`)
2. Add a new entry to the `rules` array:

```json
{
  "id": "JS010",
  "name": "no_alert_calls",
  "description": "alert() should never be used in production code",
  "severity": "error",
  "category": "debug",
  "type": "regex",
  "pattern": "\\balert\\s*\\(",
  "message": "alert() call detected. Remove before pushing.",
  "fix_suggestion": "Remove the alert() call or replace with a proper notification component.",
  "file_extensions": [".js", ".jsx", ".ts", ".tsx"],
  "exclude_file_patterns": ["*.test.*", "*.spec.*"],
  "enabled": true
}
```

3. Verify it loads correctly:
```bash
cra rules --lang javascript
```

---

## AI Checks (YAML)

The AI deep review is controlled by `agent/ai/ai_checks.yaml`. This file defines what Groq checks for — separate from the JSON rules.

### File location

```
agent/ai/ai_checks.yaml
```

### Structure

```yaml
checks:
  - name: Check Name
    description: >
      What the AI should look for.
      Can be multiple lines.
      Be specific — the AI follows these instructions exactly.
```

### Current checks

| Check | What it looks for |
|-------|------------------|
| `Security` | Hardcoded secrets, XSS, SQL injection, CSRF, JWT issues |
| `Folder Structure` | B4G standards for Express, FastAPI, React, Next.js, React Native |
| `Duplicate Code` | Repeated logic, copy-pasted blocks, DRY violations |
| `Large Functions` | Functions longer than 40 lines that should be split |
| `Large Files` | Files over 300 lines that need splitting |
| `Error Handling` | Missing try/catch, bare exceptions, exposed stack traces |
| `Performance` | N+1 queries, blocking calls, missing pagination |
| `Naming Conventions` | camelCase, PascalCase, snake_case, route naming |
| `Dead Code` | Unused imports, variables, commented-out code |
| `Code Quality` | DRY, KISS, SOLID, import order |
| `Missing Files` | Missing `.env.example`, `README.md`, `.gitignore`, test files |

### Adding a new AI check

Open `agent/ai/ai_checks.yaml` and add a new entry:

```yaml
checks:
  - name: API Response Format
    description: >
      All API responses must follow the standard format:
      { success: boolean, message: string, data?: any, code?: string }.
      Flag any route handler that returns a response not matching this format.
      Check for missing status codes — every response must set an HTTP status code explicitly.
```

The AI will include this check on every review automatically.

### Disabling a check

Simply remove the entry or comment it out:

```yaml
checks:
  - name: Security
    description: >
      ...

  # - name: Dead Code    ← commented out, won't be checked
  #   description: >
  #     ...
```

---

## AI Response Output

When `--ai` is used, the AI review shows a structured report in the terminal:

```
── AI Deep Review (Groq) ────────────────────────────────────────
[AI] Analysing 2 file(s) — please wait...

  Quality Score: 7 / 10

  The code is generally well-structured but has some security concerns
  around input validation and a few functions that are too large.

  ● HIGH  (Critical / Security / Breaking)

    📄 src/controllers/userController.ts:45  [security]
    Problem: User input is passed directly to the database query without validation.
    Fix:     Use Joi or Zod to validate req.body before processing.

  ● MEDIUM  (Performance / Maintainability)

    📄 src/routes/auth.ts:120  [function_too_large]
    Problem: loginHandler is 85 lines — too large for a single function.
    Fix:     Extract token generation and email sending into separate helper functions.

  ● LOW  (Style / Minor Improvements)

    📄 src/utils/helpers.ts:12  [naming]
    Problem: Variable 'userData' should be 'userdata' per naming conventions.
    Fix:     Rename to camelCase: userData → userData (already correct) or check convention.

  ● Large Files (consider splitting):

    📄 src/controllers/userController.ts  (~420 lines)
    Suggestion: Split into userAuthController.ts and userProfileController.ts

  ● Large Functions (consider splitting):

    ƒ  loginHandler()  in src/routes/auth.ts
    Problem: Handles authentication, token generation, and email all in one function.
    Fix:     Extract into authenticateUser(), generateToken(), sendWelcomeEmail()

  ● Duplicate Code / Redundancy:

    ⟳  JWT token validation logic duplicated
    Locations: src/routes/auth.ts:34, src/middlewares/authMiddleware.ts:18
    Fix:       Extract into a shared verifyToken() utility in src/utils/jwt.ts

  ● Folder Structure Issues:
    →  helpers.ts should be inside src/helpers/ not src/utils/ per B4G Express standards

  Files MISSING from repo:
    +  .env.example
    +  README.md

  .gitignore corrections:
    →  Add: .env
    →  Add: node_modules/

  Quick wins:
    ✓  Remove 3 unused imports in userController.ts
    ✓  Add input validation middleware to /login route

  Major production risks:
    ⚠  Unvalidated user input on line 45 could allow NoSQL injection

  Refactoring roadmap:
    Step 1: Add input validation to all routes (security critical)
    Step 2: Split userController.ts into separate files
    Step 3: Extract duplicate JWT logic into shared utility

  ────────────────────────────────────────────────────────────────
  1 high  |  1 medium  |  1 low  |  3 total issue(s)
  🚫  High severity issues found — fix before merging.
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | No blocking issues — commit goes through |
| `1` | High severity issues found — commit is blocked |
| `2` | Configuration or usage error |

---

## Supported Languages & Frameworks

| Language | Frameworks |
|----------|-----------|
| Python | FastAPI, Django, Flask |
| JavaScript | Express/Node.js, React, Next.js, React Native, Vue, Angular |
| TypeScript | Express/Node.js, React, Next.js, React Native |

Language and framework are **auto-detected** from your project files. You can override them:

```bash
cra review --lang typescript --framework express
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Groq API key for AI review (set automatically during `cra install`) |
| `GEMINI_API_KEY` | Gemini API key (fallback if Groq key not set) |
| `OPENAI_API_KEY` | OpenAI API key (fallback if Gemini key not set) |
| `ANTHROPIC_API_KEY` | Anthropic/Claude API key (fallback if OpenAI key not set) |

AI provider priority: **Groq → Gemini → OpenAI → Claude**
