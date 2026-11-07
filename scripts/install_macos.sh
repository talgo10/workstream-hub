#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "==> [1/6] Installing workstream-hub CLI/MCP/UI tools via uv..."
uv tool install --force --editable "$REPO_ROOT"

echo "==> [2/6] Initializing ~/.workstreams runtime hierarchy & SQLite WAL state.db..."
mkdir -p "$HOME/.workstreams/bin"
cp -f "$REPO_ROOT/scripts/check_file_size_hook.py" "$HOME/.workstreams/bin/check_file_size_hook.py"
chmod +x "$HOME/.workstreams/bin/check_file_size_hook.py"
uv run --directory "$REPO_ROOT" ws init --warm-embedder

echo "==> [3/6] Symlinking skills into ~/.copilot/skills/ and ~/.omnigent/skills/..."
mkdir -p "$HOME/.copilot/skills" "$HOME/.omnigent/skills"
for skill_dir in "$REPO_ROOT/assets/skills/"*; do
    if [ -d "$skill_dir" ]; then
        skill_name="$(basename "$skill_dir")"
        ln -sfn "$skill_dir" "$HOME/.copilot/skills/$skill_name"
        ln -sfn "$skill_dir" "$HOME/.omnigent/skills/$skill_name"
    fi
done

echo "==> [4/6] Copying shunt_policy.yaml into ~/.omnigent/policies/..."
mkdir -p "$HOME/.omnigent/policies"
cp -f "$REPO_ROOT/assets/omnigent/shunt_policy.yaml" "$HOME/.omnigent/policies/shunt_policy.yaml"

echo "==> [5/6] Symlinking VS Code prompts into ~/Library/Application Support/Code/User/prompts/..."
VSCODE_PROMPTS_DIR="$HOME/Library/Application Support/Code/User/prompts"
mkdir -p "$VSCODE_PROMPTS_DIR"
for prompt_file in "$REPO_ROOT/assets/prompts/"*.prompt.md; do
    if [ -f "$prompt_file" ]; then
        ln -sfn "$prompt_file" "$VSCODE_PROMPTS_DIR/$(basename "$prompt_file")"
    fi
done

echo "==> [6/6] Installing macOS LaunchAgent for http://localhost:8765 Web Cockpit..."
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR"
PLIST_DEST="$LAUNCH_AGENTS_DIR/com.workstreams.ui.plist"
cp -f "$REPO_ROOT/scripts/com.workstreams.ui.plist" "$PLIST_DEST"
launchctl unload "$PLIST_DEST" >/dev/null 2>&1 || true
launchctl load "$PLIST_DEST" >/dev/null 2>&1 || true

echo "==> Workstream Hub installation complete!"
echo "    - CLI:         ws --help"
echo "    - MCP Server:  ws-mcp"
echo "    - Web Cockpit: http://localhost:8765"

