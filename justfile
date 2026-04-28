# PKP - Personal Knowledge Pipeline
# Justfile for common development commands

# Default target - show help
default:
    @just --list

# =============================================================================
# Code Quality (lint, format, type check, security, complexity)
# =============================================================================

# Run all code quality checks
#
# Note:
# lxml 5.x is currently required by Crawl4AI (<6 constraint),
# while pip-audit flags CVE-2026-41066 with fix version in lxml 6.x.
# Since upstream dependency resolution blocks upgrading to lxml>=6,
# we temporarily ignore this CVE until Crawl4AI releases support.
#
# pip-audit supports ignoring specific vulnerabilities via:
# --ignore-vuln CVE-XXXX-YYYY :contentReference[oaicite:0]{index=0}
check:
    uv run ruff check pkp/
    uv run ruff format --check pkp/
    uv run mypy pkp/
    uv run bandit -c pyproject.toml -r pkp/
    uv run xenon --max-absolute C --max-modules C --max-average A pkp/
    uv run pip-audit \
        --ignore-vuln CVE-2026-1839 \
        --ignore-vuln CVE-2026-41066 \
        --ignore-vuln CVE-2026-3219

# Run ruff linter
lint:
    uv run ruff check pkp/

# Run ruff formatter check
format-check:
    uv run ruff format --check pkp/

# Run ruff formatter (fix)
format:
    uv run ruff format pkp/

# Run mypy type checking
typecheck:
    uv run mypy pkp/

# Run bandit security scanner
security:
    uv run bandit -c pyproject.toml -r pkp/

# Run radon complexity analysis
complexity:
    uv run radon cc -a -i pkp/ && uv run xenon --max-absolute C --max-modules C --max-average A pkp/

# Run pip-audit vulnerability scan
#
# Note: CVE-2026-41066 is in lxml 5.x (required by Crawl4AI <6).
# CVE-2026-3219 is in pip 26.x (unfixed as of 2026-04-28).
#
vulns:
    uv run pip-audit \
        --ignore-vuln CVE-2026-1839 \
        --ignore-vuln CVE-2026-41066 \
        --ignore-vuln CVE-2026-3219

# =============================================================================
# Pre-commit hooks
# =============================================================================

# Install pre-commit hooks
hook-install:
    uv run pre-commit install

# Run pre-commit hooks on all files
hook-run-all:
    uv run pre-commit run -a

# Update pre-commit hook versions
hook-update:
    uv run pre-commit autoupdate

# =============================================================================
# Testing
# =============================================================================

# Run all tests
test:
    pytest

# Run tests with coverage
test-cov:
    pytest --cov=pkp --cov-report=term-missing

# Run tests matching pattern
test-grep PATTERN:
    pytest -k "{{PATTERN}}"

# =============================================================================
# Development
# =============================================================================

# Install all dependencies
sync:
    uv sync

# Install dev dependencies
sync-dev:
    uv sync --group dev

# Add a package to dependencies
add PACKAGE:
    uv add "{{PACKAGE}}"

# Add a dev package
add-dev PACKAGE:
    uv add --group dev "{{PACKAGE}}"

# Remove a package
remove PACKAGE:
    uv remove "{{PACKAGE}}"

# Upgrade a package
upgrade PACKAGE:
    uv sync --upgrade-package "{{PACKAGE}}"

# =============================================================================
# Running the application
# =============================================================================

# Run the CLI
run *ARGS:
    pkp {{ARGS}}

# Run the API server
serve:
    uvicorn pkp.api.app:app --reload

# Open API docs
docs:
    open http://localhost:8000/docs

# =============================================================================
# Database
# =============================================================================

# Initialize the database
db-init:
    pkp init

# Show database info
db-info:
    pkp info

# =============================================================================
# Utility
# =============================================================================

# Clean cache files
clean:
    rm -rf .ruff_cache .mypy_cache .pytest_cache
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete

# Show lock file dependencies
deps-tree:
    uv tree
