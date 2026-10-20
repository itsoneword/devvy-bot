.PHONY: install dev test test-core lint format clean help run run-debug \
       env-example env-example-check \
       bump-patch bump-minor bump-major release version

# Default target
help:
	@echo "Available commands:"
	@echo "  install       - Install production dependencies"
	@echo "  dev           - Install development dependencies"
	@echo "  test          - Run tests"
	@echo "  test-core     - Run the fast core subset (before every restart, ~1 min)"
	@echo "  lint          - Run linting checks"
	@echo "  format        - Format code"
	@echo "  clean         - Clean up generated files"
	@echo "  run           - Run the bot"
	@echo "  env-example   - Regenerate .env.example from src/config/settings.py"
	@echo "  version       - Show current version"
	@echo "  bump-patch    - Bump patch version (1.2.0 -> 1.2.1), commit, and tag"
	@echo "  bump-minor    - Bump minor version (1.2.0 -> 1.3.0), commit, and tag"
	@echo "  bump-major    - Bump major version (1.2.0 -> 2.0.0), commit, and tag"
	@echo "  release       - Push current version tag to trigger release workflow"

install:
	poetry install --no-dev

dev:
	poetry install
	poetry run pre-commit install --install-hooks || echo "pre-commit not configured yet"

test:
	poetry run pytest

# Core mechanics (window, evaluator, items, policy parity, export drift); the
# list lives in tests/conftest.py CORE_TESTS. Run before every restart.
test-core:
	poetry run pytest -m core --no-cov -q -p no:cacheprovider

lint:
	poetry run python scripts/gen_env_example.py --check
	poetry run black --check src tests
	poetry run isort --check-only src tests
	poetry run flake8 src tests
	poetry run mypy src

format:
	poetry run black src tests
	poetry run isort src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

run:
	poetry run devvy

# For debugging
run-debug:
	poetry run devvy --debug

# --- Generated files ---

env-example:  ## Regenerate .env.example from src/config/settings.py
	poetry run python scripts/gen_env_example.py

env-example-check:  ## Fail if .env.example has drifted from settings.py
	poetry run python scripts/gen_env_example.py --check

# --- Version Management ---

version:  ## Show current version
	@poetry version -s

bump-patch:  ## Bump patch version, commit, and tag
	poetry version patch && \
	NEW_VERSION=$$(poetry version -s) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

bump-minor:  ## Bump minor version, commit, and tag
	poetry version minor && \
	NEW_VERSION=$$(poetry version -s) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

bump-major:  ## Bump major version, commit, and tag
	poetry version major && \
	NEW_VERSION=$$(poetry version -s) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

release:  ## Push the current version tag to trigger the release workflow
	CURRENT_VERSION=$$(poetry version -s) && \
	git push && git push origin "v$$CURRENT_VERSION" && \
	echo "Pushed v$$CURRENT_VERSION. Release workflow will run on GitHub."
