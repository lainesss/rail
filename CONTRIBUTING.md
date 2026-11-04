# Contributing to Rail

## Setup

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and
Python 3.14, then run from the repository root:

```bash
uv sync --locked --dev --python 3.14
uv run --no-sync pre-commit install
```

Work on a feature branch. The Git hook runs checks on `git commit`; some checks
format files, so review their changes before committing again.

See [tests/README.md](tests/README.md) for test commands.

## Issues and pull requests

For bugs, include the command, expected and actual behavior, and relevant tool
versions. Remove credentials and private details from logs.

Keep PRs focused, explain the change and how you verified it, and update tests
and documentation when behavior changes. CI checks pull requests.

For contributions from a fork, open the PR through GitHub or GitHub CLI;
`rail finish` supports branches within one repository.
