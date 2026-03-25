#!/bin/bash
# Code Review Agent — one-command installer for Mac/Linux
# Usage: bash install.sh  (run from inside your project directory)

set -e

REPO="https://github.com/pratham-b4g/code_review_agent.git"

echo ""
echo "=============================================="
echo "  Code Review Agent — Installer"
echo "=============================================="
echo ""

# Step 1: Install the package
echo "[1/2] Installing cra package..."
pip install "git+${REPO}" --quiet
echo "      Done."

# Step 2: Install the hook in CWD if it's a git repo
echo "[2/2] Installing pre-commit hook..."
if git rev-parse --git-dir > /dev/null 2>&1; then
    cra install
else
    echo "      Warning: not inside a git repo — skipping hook install."
    echo "      Run 'cra install' manually from inside your project."
fi

echo ""
echo "  All done! Every git commit will now be reviewed automatically."
echo ""
