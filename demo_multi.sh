#!/bin/bash
# Demo: two Claude Code workers collaborating on a shared git repo
#
# Creates a toy project, clones it twice, and launches termiclaude-multi
# with two workers: one builds the API, another writes tests.
#
# Usage:
#   ./demo_multi.sh           # real Claude Code (needs claude on PATH)
#   ./demo_multi.sh --dry-run # show what would happen without running claude

set -e

DEMO_DIR="/tmp/termiclaude-multi-demo"
EXTRA_ARGS="${@}"

echo "=== termiclaude multi-worker demo ==="
echo ""

# Clean up previous demo
rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"

# 1. Create the "origin" repo
echo "[1/4] Creating git repo..."
mkdir -p "$DEMO_DIR/origin"
cd "$DEMO_DIR/origin"
git init -q
git checkout -b main 2>/dev/null || true

cat > app.py << 'EOF'
"""Simple Flask app — needs CRUD endpoints and tests."""
from flask import Flask

app = Flask(__name__)

# TODO: Add User model
# TODO: Add CRUD endpoints (GET/POST/PUT/DELETE /users)
# TODO: Add input validation
# TODO: Add error handling

if __name__ == '__main__':
    app.run(debug=True)
EOF

cat > requirements.txt << 'EOF'
flask>=3.0
pytest>=8.0
EOF

cat > README.md << 'EOF'
# Demo Project

Simple Flask CRUD API for users. Two Claude Code agents will work on this:
- **api-worker**: implements the CRUD endpoints in app.py
- **test-worker**: writes pytest tests in test_app.py

They coordinate through the termiclaude foreman.
EOF

git add -A
git commit -q -m "Initial scaffold: empty Flask app"

# 2. Clone into two working directories
echo "[2/4] Cloning into two working dirs..."
git clone -q "$DEMO_DIR/origin" "$DEMO_DIR/worker-api"
git clone -q "$DEMO_DIR/origin" "$DEMO_DIR/worker-tests"

# Configure git user in both clones
for dir in "$DEMO_DIR/worker-api" "$DEMO_DIR/worker-tests"; do
    cd "$dir"
    git config user.name "demo"
    git config user.email "demo@test"
done

echo "[3/4] Directory structure:"
echo "  origin:       $DEMO_DIR/origin"
echo "  worker-api:   $DEMO_DIR/worker-api"
echo "  worker-tests: $DEMO_DIR/worker-tests"
echo ""

# 3. Install termiclaude if needed
cd "$(dirname "$0")"
if ! command -v termiclaude-multi &>/dev/null; then
    echo "[3.5/4] Installing termiclaude..."
    pip install -e . -q
fi

# 4. Launch multi-worker
echo "[4/4] Launching termiclaude multi-worker..."
echo ""
echo "  Two Claude Code agents will start:"
echo "    [api]   → implements CRUD endpoints in app.py"
echo "    [tests] → writes pytest tests in test_app.py"
echo ""
echo "  Foreman pane at bottom — use /send, /broadcast, /status"
echo "  Press Ctrl-B then d to detach from tmux"
echo ""

exec termiclaude-multi \
    --worker "api:$DEMO_DIR/worker-api:Implement a Flask CRUD API for users in app.py. Add a User model (in-memory dict), GET /users, GET /users/<id>, POST /users, PUT /users/<id>, DELETE /users/<id>. Add input validation and error handling. Do NOT write tests." \
    --worker "tests:$DEMO_DIR/worker-tests:Write comprehensive pytest tests in test_app.py for the Flask CRUD API. Import app from app.py. Test all endpoints: GET /users (empty and populated), POST /users (valid and invalid), PUT /users, DELETE /users. Use Flask test client. Wait for the api worker to finish the endpoints first — check app.py before writing tests." \
    $EXTRA_ARGS
