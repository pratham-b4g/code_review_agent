# Code Review Agent - Changes and Implementation Details

## Overview
This document tracks all the changes made to the code review agent to fix various issues including:
- Branch dropdown functionality
- Duplication detection and display
- Warning/info/error display in UI
- Unused variable/function detection
- Analytics accumulation issues

## Changes Made

### 1. Branch Selection & Switching

#### Backend (server.py)
**Problem:** Two separate API endpoints for scanning - one with branch, one without.

**Solution:** Merged into single endpoint that checks for branch parameter:
```python
# Check if branch parameter is specified
qs = parse_qs(parsed.query)
branch = qs.get("branch", [None])[0]

if branch:
    result = self._scan_project_branch(project_path, project_id, _current_user["email"], branch)
else:
    result = self._scan_project(project_path, project_id, _current_user["email"])
```

**Key Fix:** Fixed branch name sanitization for temp directories:
```python
safe_branch = branch.replace('/', '_')
temp_dir = tempfile.mkdtemp(prefix=f'cra_scan_{safe_branch}_')
```

#### Frontend (multi_user.html)
**Problem:** Branch dropdown not showing selected branch after switching.

**Solution:** 
1. Modified `fetchProjectBranches()` to accept `selectedBranch` parameter:
```javascript
async function fetchProjectBranches(projectId, selectedBranch) {
  // ...
  const activeBranch = selectedBranch || 'main';
  select.innerHTML = branches.map(branch => 
    `<option value="${branch}" ${branch === activeBranch ? 'selected' : ''}>${branch}</option>`
  ).join('');
}
```

2. Updated `switchReviewBranch()` to call with selected branch:
```javascript
await fetchProjectBranches(projectId, branch);
```

---

### 2. Duplication Detection & Display

#### Backend (server.py)
**Problem:** Duplication stats not being returned to frontend.

**Solution:** Added duplication object to scan result:
```python
"duplication": {
    "percentage": dup_stats.percentage,
    "duplicated_lines": dup_stats.duplicated_lines,
    "total_lines": dup_stats.total_lines
}
```

**Note:** `DuplicationStats` class has a `percentage` property (not `duplication_percentage`).

#### Frontend (multi_user.html)
**Problem:** Duplication tab showing count instead of percentage.

**Solution:** 
1. Updated stats display to show percentage:
```javascript
<div class="text-xl font-bold text-accent-purple">${(currentReviewData.duplication || {}).percentage || 0}%</div>
```

2. Added duplication view panel with stats:
```javascript
function showDuplicationView() {
  // Update stats
  const dup = currentReviewData.duplication || {};
  document.getElementById('dupPercentage').textContent = (dup.percentage || 0).toFixed(1) + '%';
  document.getElementById('dupLines').textContent = dup.duplicated_lines || 0;
  document.getElementById('dupTotal').textContent = dup.total_lines || 0;
  // ... show violations list
}
```

---

### 3. Warning/Error/Info Display Issues

#### Problem 1: Path Matching for ESLint Violations
**Issue:** ESLint returns absolute paths (e.g., `C:\Temp\cra_scan_xxx\app\page.tsx`), but file tree uses relative paths (e.g., `app/page.tsx`).

**Solution:** Convert ESLint paths to relative in `_run_eslint_json`:
```python
# Convert absolute path to relative path for consistency
rel_path = file_path
if temp_dir and file_path.startswith(temp_dir):
    rel_path = file_path[len(temp_dir):].lstrip('/\\')
elif project_root and file_path.startswith(project_root):
    rel_path = file_path[len(project_root):].lstrip('/\\')
```

Also fixed `make_relative` function to handle Windows path separators:
```python
def make_relative(path):
    import os
    norm_path = path.replace('\\', '/')
    norm_temp = temp_dir.replace('\\', '/') if temp_dir else ""
    if norm_temp and norm_path.startswith(norm_temp):
        return norm_path[len(norm_temp):].lstrip('/')
    return path.replace('\\', '/')
```

#### Problem 2: Orphaned Violations (File Not in Tree)
**Issue:** Some violations don't match any file in the tree because of path mismatches.

**Solution:** Added "Project Issues" section in file tree:
```javascript
function buildFileTree(files, violations) {
  const tree = { type: 'root', children: {}, violations: [], orphanedViolations: [] };
  // ... build tree
  // Collect violations that don't match any file
  if (!matched) {
    tree.orphanedViolations.push(v);
  }
}
```

Added `showProjectIssues()` function to display orphaned violations.

---

### 4. Unused Variable/Function Detection

#### Backend (server.py)
**Problem:** ESLint not detecting unused variables.

**Solution:** 
1. Ensure ESLint config has `unused-imports` plugin:
```python
def _ensure_eslint_for_scan(self, project_root: str) -> str:
    # Always update/create .eslintrc.json with unused-imports rules
    config["rules"]["unused-imports/no-unused-imports"] = "error"
    config["rules"]["unused-imports/no-unused-vars"] = [
        "warn",
        {"vars": "all", "varsIgnorePattern": "^_", "args": "after-used", "argsIgnorePattern": "^_"}
    ]
```

2. Added taint analysis for Python projects:
```python
if lang == 'python':
    from agent.analyzer.taint_analyzer import run_taint_analysis
    for f in files:
        if f.endswith('.py'):
            taint_v = run_taint_analysis(f, src)
            result.violations.extend(taint_v)
```

#### Frontend (multi_user.html)
**Problem:** Not seeing all warnings in UI.

**Solution:** Added debug logging to trace violations:
```javascript
console.log('[CodeReview] Violations received:', violations.length);
console.log('[CodeReview] Errors:', violations.filter(v => v.severity === 'error').length);
console.log('[CodeReview] Warnings:', violations.filter(v => v.severity === 'warning').length);
console.log('[CodeReview] Infos:', violations.filter(v => v.severity === 'info').length);
console.log('[CodeReview] ESLint violations:', violations.filter(v => v.rule_id && v.rule_id.includes('eslint')).length);
console.log('[CodeReview] Unused variable violations:', violations.filter(v => v.rule_id && v.rule_id.includes('unused')).length);
```

---

### 5. Analytics Accumulation Issue

#### Backend (db_manager.py)
**Problem:** Analytics issues count keeps increasing every scan (320 instead of 20).

**Solution:** Modified upsert logic to replace values for scans (commits_count=0):
```sql
ON CONFLICT (user_email, project_id, branch, date)
DO UPDATE SET
    issues_found = CASE WHEN EXCLUDED.commits_count = 0 
        THEN EXCLUDED.issues_found 
        ELSE developer_analytics.issues_found + EXCLUDED.issues_found END,
    code_quality_score = EXCLUDED.code_quality_score
```

Scans now replace the daily count, while git commits accumulate.

---

### 6. Severity Filter for File Tree

#### Frontend (multi_user.html)
**Problem:** Info filter and file tree badges not working correctly.

**Solution:** 
1. Added info count to file badges:
```javascript
const infos = hasViolations ? node.violations.filter(v => v.severity === 'info').length : 0;
// ...
${infos > 0 ? `<span class="text-xs bg-accent-blue/20 text-accent-blue px-1.5 rounded">${infos}</span>` : ''}
```

2. Updated filter logic to check severity match:
```javascript
const matchesFilter = severityFilter === 'all' || 
  (hasViolations && node.violations.some(v => v.severity === severityFilter));
```

---

## Debug Logging

### Backend (server.py)
Added extensive debug logging:
```python
print(f"[Scan] Loaded {len(rules)} rules")
print(f"[Scan] AST rules: {[r.get('id') for r in ast_rules]}")
print(f"[Scan] Duplication stats: {dup_stats.duplicated_lines} / {dup_stats.total_lines} lines ({dup_stats.percentage:.1f}%)")
print(f"[Scan] ESLint found {len(eslint_violations)} violations")
print(f"[Scan] Warning: Violation file not in file list: {rel_file}")
```

### Frontend (multi_user.html)
Added browser console logging:
```javascript
console.log('[CodeReview] Violations received:', violations.length);
console.log('[CodeReview] Duplication stats:', result.duplication);
console.log('[CodeReview] Sample violations:', violations.slice(0, 3));
```

---

## Installation Commands

### From GitHub (Production)
```bash
pip install git+https://github.com/akash150897/cra_panel.git
```

### Local Development (Editable)
```bash
pip install -e .
```

### Force Reinstall (Clear Cache)
```bash
pip install --force-reinstall --no-cache-dir git+https://github.com/akash150897/cra_panel.git
```

---

## Testing Checklist

After installing, verify:

1. **Branch Switching**
   - Select different branch from dropdown
   - Verify dropdown shows selected branch name
   - Verify scan runs on correct branch

2. **Duplication Display**
   - Check duplication percentage shows correct value (not 0%)
   - Click duplication button to see details
   - Verify "X duplicated lines / Y total lines" shows correctly

3. **Violation Display**
   - Open browser console (F12)
   - Check `[CodeReview]` logs for violation counts
   - Verify file tree shows correct error/warning/info badges
   - Click on files to see violations in issues panel

4. **ESLint Warnings**
   - Check console for ESLint violations count
   - Verify unused variables/functions appear in UI
   - Check terminal for `[ESLint]` debug messages

5. **Analytics**
   - Run scan multiple times
   - Verify issues count doesn't accumulate (should stay same)

---

## Known Issues & Fixes

| Issue | Cause | Fix |
|-------|-------|-----|
| Duplication shows 0% | `duplication_percentage` attribute doesn't exist | Use `percentage` property |
| Branch dropdown shows 'main' | Timeout issue in setting value | Set immediately after render |
| ESLint warnings not showing | Path mismatch between ESLint and file tree | Convert ESLint paths to relative |
| Analytics accumulating | Upsert adds instead of replaces | Check `commits_count` in upsert logic |
| Orphaned violations | Files not matching violation paths | Added "Project Issues" section |

---

## Version History

- **2.4.1**: Fixed duplication stats, branch dropdown, warning display, analytics accumulation
- **2.4.0**: Initial implementation of branch-based scanning

---

## Contributors
- Akash Kothari (B4G Projects)

## License
Proprietary - B4G Projects
