.PHONY: install test lint run-ui run-mcp init clean

install:
	chmod +x scripts/install_macos.sh scripts/check_file_size_hook.py
	./scripts/install_macos.sh

test:
	uv run pytest -v

init:
	uv run ws init --warm-embedder

run-ui:
	uv run ws-ui

run-mcp:
	uv run ws-mcp

clean:
	rm -rf .pytest_cache dist build *.egg-info
	find . -name "__pycache__" -type d -prune -exec rm -rf '{}' +

