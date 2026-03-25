# Code Review Agent — one-command installer for Windows (PowerShell)
# Usage: Run this from inside your project directory
#   .\install.ps1
# Or run remotely (no download needed):
#   iex (iwr "https://raw.githubusercontent.com/pratham-b4g/code_review_agent/master/install.ps1").Content

$REPO = "https://github.com/pratham-b4g/code_review_agent.git"

Write-Host ""
Write-Host "=============================================="
Write-Host "  Code Review Agent — Installer"
Write-Host "=============================================="
Write-Host ""

# Step 1: Install the package
Write-Host "[1/2] Installing cra package..."
pip install "git+$REPO" --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: pip install failed. Make sure Python and pip are installed." -ForegroundColor Red
    exit 1
}
Write-Host "      Done."

# Step 2: Install the hook in CWD if it's a git repo
Write-Host "[2/2] Installing pre-commit hook..."
$gitCheck = git rev-parse --git-dir 2>$null
if ($LASTEXITCODE -eq 0) {
    cra install
} else {
    Write-Host "      Warning: not inside a git repo — skipping hook install." -ForegroundColor Yellow
    Write-Host "      Run 'cra install' manually from inside your project."
}

Write-Host ""
Write-Host "  All done! Every git commit will now be reviewed automatically." -ForegroundColor Green
Write-Host ""
