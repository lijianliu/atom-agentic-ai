.PHONY: install lock update-requirements

ENV_FILE := $(HOME)/.config/atom-agentic-ai/env.sh
SOURCE_ENV := $(if $(wildcard $(ENV_FILE)),source $(ENV_FILE) &&,)

## Generate local uv.lock for your environment
lock:
	@bash -c '$(SOURCE_ENV) uv lock'

## Install deps (run `lock` first on a fresh clone)
install:
	@bash -c '$(SOURCE_ENV) uv sync --all-groups'

## Regenerate requirements.txt from current uv.lock
update-requirements:
	@bash -c '$(SOURCE_ENV) uv export --format requirements-txt --no-dev --no-emit-project > requirements.txt'
	@echo ">> requirements.txt updated. Commit it."
