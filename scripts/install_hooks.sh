#!/bin/sh
# Installs git hooks that run the secret scanner before every commit and every push.
set -e
cd "$(dirname "$0")/.."
[ -d .git ] || { echo "No .git folder here. Run 'git init' first."; exit 1; }
cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/sh
PY=$(command -v python3 || command -v python) || exit 0
exec "$PY" scripts/check_secrets.py --staged
HOOK
cat > .git/hooks/pre-push <<'HOOK'
#!/bin/sh
PY=$(command -v python3 || command -v python) || exit 0
exec "$PY" scripts/check_secrets.py
HOOK
chmod +x .git/hooks/pre-commit .git/hooks/pre-push
echo "Hooks installed: secrets are checked before every commit and push."
