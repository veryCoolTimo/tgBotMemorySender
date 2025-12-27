#!/bin/bash

echo "=== MemoryBase Bot Setup Check ==="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

ok() { echo -e "${GREEN}[OK]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }

# 1. Check tg-bot repo
echo "1. Checking tg-bot repo..."
cd ~/tgbotforgit/tg-bot 2>/dev/null
if [ $? -eq 0 ]; then
    BRANCH=$(git branch --show-current)
    echo "   Branch: $BRANCH"
    if [ "$BRANCH" = "mcp-for-claude-code" ]; then
        ok "Correct branch"
    else
        fail "Wrong branch! Should be mcp-for-claude-code"
    fi

    # Check if git pull --rebase is in bot.py
    if grep -q "git.*pull.*rebase" bot.py 2>/dev/null; then
        ok "git pull --rebase found in bot.py"
    else
        fail "git pull --rebase NOT found in bot.py"
    fi
else
    fail "tg-bot directory not found"
fi

echo ""

# 2. Check memoryBase repo
echo "2. Checking memoryBase repo..."
cd ~/tgbotforgit/memoryBase 2>/dev/null
if [ $? -eq 0 ]; then
    BRANCH=$(git branch --show-current)
    echo "   Branch: $BRANCH"
    if [ "$BRANCH" = "main" ]; then
        ok "Correct branch (main)"
    else
        fail "Wrong branch! Should be main"
    fi

    # Check if detached HEAD
    if git symbolic-ref -q HEAD >/dev/null; then
        ok "Not in detached HEAD"
    else
        fail "In detached HEAD state!"
    fi

    # Check pull.rebase config
    REBASE=$(git config pull.rebase)
    if [ "$REBASE" = "true" ]; then
        ok "pull.rebase = true"
    else
        fail "pull.rebase not set! Run: git config pull.rebase true"
    fi

    # Check if up to date with origin
    git fetch origin 2>/dev/null
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main)
    if [ "$LOCAL" = "$REMOTE" ]; then
        ok "Up to date with origin/main"
    else
        fail "Not up to date! Run: git pull"
    fi

    # Check for uncommitted changes
    if git diff --quiet && git diff --staged --quiet; then
        ok "No uncommitted changes"
    else
        fail "Has uncommitted changes!"
    fi
else
    fail "memoryBase directory not found"
fi

echo ""

# 3. Check .env
echo "3. Checking .env..."
cd ~/tgbotforgit/tg-bot
if [ -f .env ]; then
    ok ".env file exists"

    if grep -q "API_PORT" .env; then
        ok "API_PORT configured"
    else
        fail "API_PORT not in .env"
    fi

    if grep -q "API_SECRET" .env; then
        ok "API_SECRET configured"
    else
        fail "API_SECRET not in .env"
    fi

    REPO_PATH=$(grep REPO_PATH .env | cut -d'=' -f2)
    echo "   REPO_PATH: $REPO_PATH"
    if [ -d "$REPO_PATH" ]; then
        ok "REPO_PATH directory exists"
    else
        fail "REPO_PATH directory does not exist!"
    fi
else
    fail ".env file not found"
fi

echo ""

# 4. Check API
echo "4. Checking API..."
HEALTH=$(curl -s http://localhost:8585/health 2>/dev/null)
if echo "$HEALTH" | grep -q "ok"; then
    ok "API health endpoint works"
else
    fail "API not responding"
fi

echo ""

# 5. Test git operations in memoryBase
echo "5. Testing git operations..."
cd ~/tgbotforgit/memoryBase

# Try git pull
echo "   Testing git pull..."
PULL_OUTPUT=$(git pull --rebase origin main 2>&1)
if [ $? -eq 0 ]; then
    ok "git pull works"
else
    fail "git pull failed: $PULL_OUTPUT"
fi

# Create test file
echo "   Testing commit & push..."
echo "test $(date)" > /tmp/memorybase_test.txt
cp /tmp/memorybase_test.txt test_file.md
git add test_file.md
git commit -m "test commit from check script" >/dev/null 2>&1

if git push 2>&1; then
    ok "git push works"
    # Cleanup
    git rm test_file.md >/dev/null 2>&1
    git commit -m "cleanup test file" >/dev/null 2>&1
    git push >/dev/null 2>&1
else
    fail "git push failed!"
    git reset --hard HEAD~1 >/dev/null 2>&1
fi

echo ""
echo "=== Check Complete ==="
