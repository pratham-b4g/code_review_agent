# Code Review Agent - Project Scope

## Project Overview

The **Code Review Agent (CRA)** is an intelligent, Python-based code quality analysis tool designed to automate code reviews for development teams. It integrates with Git repositories to analyze code changes before commits, providing real-time feedback on code quality, security vulnerabilities, and best practices.

## Core Purpose

CRA serves as a **Git pre-push hook** and **web-based dashboard** that:
- Automatically analyzes code changes before they are pushed to remote repositories
- Identifies bugs, security issues, and code quality problems
- Enforces coding standards and best practices
- Provides actionable fix suggestions
- Tracks code quality metrics over time

## Key Features

### 1. Multi-Language Support
- **JavaScript/TypeScript**: ESLint integration, AST analysis, cross-file duplicate detection
- **Python**: AST-based analysis, taint analysis for security vulnerabilities
- **Framework Detection**: Automatic detection of React, Next.js, Vue, Django, Flask, etc.

### 2. Code Quality Analysis
- **Syntax Errors**: Parse-time error detection
- **Security Issues**: SQL injection, XSS, hardcoded secrets, unsafe eval
- **Code Duplication**: Cross-file duplicate code block detection
- **Unused Code**: Dead code elimination (unused imports, variables, functions)
- **Best Practices**: Missing error handling, architecture violations, missing tests

### 3. Real-Time Dashboard
- **Web Interface**: Modern React-like dashboard built with vanilla JS
- **Multi-User Support**: Role-based access (Admin, Team Lead, Developer)
- **Project Management**: Add/remove projects, assign users
- **Branch-Based Scanning**: Scan specific Git branches
- **File Tree Navigation**: Click-to-view code with syntax highlighting
- **Issue Panel**: Severity-filtered issues with line-by-line navigation
- **Duplication View**: Detailed duplication analysis with percentage metrics

### 4. Analytics & Reporting
- **Daily Analytics**: Track issues found, commits, code quality score
- **Developer Metrics**: Per-developer and per-project statistics
- **Trend Analysis**: Monitor code quality improvements over time
- **Export**: PDF report generation for code reviews

### 5. Git Integration
- **Pre-Push Hooks**: Analyze staged changes before push
- **Remote Repository Support**: Clone and scan GitHub/GitLab/Bitbucket repos
- **Branch Switching**: Dynamic branch selection in dashboard
- **Access Control**: Role-based project access

## Architecture

### Backend Components

```
agent/
├── dashboard/
│   └── server.py          # HTTP server, API endpoints, scan orchestration
├── analyzer/
│   ├── rule_engine.py     # Core rule execution engine
│   ├── ast_analyzer.py    # Python AST-based analysis
│   ├── js_analyzer.py     # JavaScript AST analysis
│   ├── cross_file_analyzer.py  # Cross-file duplication detection
│   └── taint_analyzer.py  # Security taint analysis
├── database/
│   └── db_manager.py      # SQLite database, analytics, user management
└── config/
    └── auth_config.py     # Authentication, JWT, role management
```

### Frontend Components

```
agent/dashboard/static/
├── multi_user.html        # Main dashboard (modern UI)
├── index.html             # Legacy dashboard (deprecated)
└── (CSS/JS embedded in HTML)
```

### Data Flow

```
Git Push / Dashboard Scan
    ↓
Clone/Checkout Branch (if remote)
    ↓
Detect Language & Framework
    ↓
Load Rules (YAML-based)
    ↓
Run AST Checks
Run ESLint (JS/TS)
Run Cross-File Analysis
Run Taint Analysis (Python)
    ↓
Deduplicate Violations
    ↓
Normalize File Paths
    ↓
Return JSON to Frontend
    ↓
Render File Tree + Issues Panel
    ↓
Interactive Code Review
```

## Target Users

1. **Individual Developers**: Pre-push code quality checks
2. **Development Teams**: Team-wide code review dashboard
3. **Team Leads**: Analytics, project management, code quality oversight
4. **DevOps/QA**: Automated quality gates in CI/CD pipelines

## Use Cases

### Use Case 1: Pre-Push Hook
```bash
$ git push
[CRA] Analyzing staged changes...
[CRA] Found 2 warnings in app/page.tsx
[CRA] ✓ Push allowed (warnings only)
```

### Use Case 2: Dashboard Scan
1. Developer logs into dashboard
2. Selects project and branch
3. Clicks "Scan Project"
4. Views file tree with issue badges
5. Clicks file to see code + issues
6. Reviews fix suggestions
7. Fixes issues locally
8. Re-scans to verify

### Use Case 3: Team Analytics
1. Team Lead opens "My Developers"
2. Views daily commit/issue metrics
3. Identifies developer with declining quality
4. Reviews their recent commits
5. Provides coaching/guidance

## Technical Stack

- **Backend**: Python 3.9+, Flask-like HTTP server
- **Frontend**: Vanilla JavaScript, Tailwind CSS-inspired styling
- **Database**: SQLite (per-project and global)
- **Analysis**: AST (ast module, esprima), ESLint, custom rule engine
- **Authentication**: JWT-based, role-based access control

## Rule System

Rules are defined in YAML format:

```yaml
- id: SQL_INJECTION
  name: SQL Injection Risk
  type: ast
  severity: error
  pattern: "cursor.execute.*%.*format"
  message: "Potential SQL injection - use parameterized queries"
  suggestion: "Use cursor.execute(query, params) instead of string formatting"

- id: UNUSED_IMPORT
  name: Unused Import
  type: ast
  severity: warning
  check: unused_imports
  message: "Unused import detected"
  fix_type: remove
```

Rule types:
- **ast**: AST-based pattern matching
- **regex**: Regex-based detection
- **cross**: Cross-file analysis (duplication, constants)

## Future Roadmap

### Phase 1 (Current)
- ✅ Core analysis engine
- ✅ Web dashboard
- ✅ Git integration
- ✅ Multi-language support
- ✅ Basic analytics

### Phase 2 (Planned)
- 🔄 IDE extensions (VS Code, PyCharm)
- 🔄 CI/CD integration (GitHub Actions, GitLab CI)
- 🔄 Team collaboration features
- 🔄 Custom rule builder UI

### Phase 3 (Vision)
- 📋 AI-powered suggestions (LLM integration)
- 📋 Auto-fix capabilities
- 📋 Performance profiling integration
- 📋 Multi-repo analysis

## Success Metrics

- **False Positive Rate**: <5%
- **Scan Performance**: <30s for 1000 files
- **User Adoption**: Daily active users
- **Issue Detection**: >90% of common bugs caught
- **Developer Satisfaction**: Reduced review time, improved code quality

## Competitive Advantage

Unlike existing tools (SonarQube, CodeClimate, ESLint alone):
- **Local-first**: No cloud dependency, works offline
- **Git-native**: Designed specifically for pre-push workflows
- **Multi-language**: Unified analysis across JS/TS/Python
- **Custom rules**: Easy YAML-based rule creation
- **Integrated dashboard**: Real-time web UI with branch support

## Project Ownership

- **Author**: Akash Kothari
- **Organization**: B4G Projects
- **License**: Proprietary
- **Repository**: https://github.com/akash150897/cra_panel.git

---

## Summary

The Code Review Agent is a comprehensive code quality tool that bridges the gap between local development workflows and team-wide code review processes. It provides automated analysis, actionable feedback, and detailed analytics to help development teams maintain high code quality standards while reducing manual review overhead.
