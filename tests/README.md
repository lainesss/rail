# Tests

From the repository root:

```bash
uv run --no-sync pytest --cov --cov-report=term-missing
```

From `tests/`:

```bash
uv run --project .. --no-sync pytest . --cov --cov-config=../pyproject.toml --cov-report=term-missing
```

Git must be on `PATH`; GitHub credentials are not required.
If overriding `--basetemp`, choose a directory outside Git checkouts.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for development setup.
