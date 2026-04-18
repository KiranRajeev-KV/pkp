# PKP - Personal Knowledge Pipeline
# Justfile for common development commands

# Default target - show help
default:
    @just --list

# =============================================================================
# Code Quality (lint, format, type check, security, complexity)
# =============================================================================

# Run all code quality checks
check:
    ruff check pkp/
    ruff format --check pkp/
    mypy pkp/
    bandit -c pyproject.toml -r pkp/
    xenon --max-absolute C --max-modules C --max-average A pkp/
    pip-audit

# Run ruff linter
lint:
    ruff check pkp/

# Run ruff formatter check
format-check:
    ruff format --check pkp/

# Run ruff formatter (fix)
format:
    ruff format pkp/

# Run mypy type checking
typecheck:
    mypy pkp/

# Run bandit security scanner
security:
    bandit -c pyproject.toml -r pkp/

# Run radon complexity analysis
complexity:
    radon cc -a -i pkp/ && xenon --max-absolute C --max-modules C --max-average A pkp/

# Run pip-audit vulnerability scan
vulns:
    pip-audit

# =============================================================================
# Pre-commit hooks
# =============================================================================

# Install pre-commit hooks
hook-install:
    pre-commit install

# Run pre-commit hooks on all files
hook-run-all:
    pre-commit run -a

# Update pre-commit hook versions
hook-update:
    pre-commit autoupdate

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
