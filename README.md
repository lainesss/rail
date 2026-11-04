# Rail

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![MIT license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A configurable Git workflow for development and automation.

Rail manages branches, isolated worktrees, project checks, GitHub pull requests,
and releases. Use it directly from the terminal, call it from scripts and
automation tools, or integrate it with coding agents through `--json` or the
[Python API](src/rail/api.py).

## Requirements

- Python **3.12+** and Git **2.38+**.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the installation
  instructions below.
- [GitHub CLI](https://cli.github.com/) (`gh`) for pull request operations.
- Git authentication for fetching and pushing; `gh auth login` for GitHub access.
- The tools used by your configured checks, installed in the target checkout's
  `.venv` or available on `PATH`.

CI covers Python 3.12–3.15 on Linux and Python 3.14 on Windows.

## Installation

Clone this repository and install Rail as an isolated command line tool:

```bash
git clone https://github.com/lainesss/rail.git
cd rail
uv tool install .
rail --version
```

For development setup, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

From the root of the project repository you want to use Rail with:

```bash
rail install
```

`rail install` asks for Git and release settings, then creates `rail.toml` with a
default Pre-commit check (an example command you can replace in `rail.toml`).
It asks before overwriting the file. Interactive setup also asks whether to track
it in Git.
Answers can also be piped through stdin.

To share configuration with new worktrees, merge or push `rail.toml` into the
configured integration branch. New worktrees start from that remote branch,
not from unpushed local changes. Then create the feature checkout:

```bash
rail worktree create --destination ../project-feature --branch add-feature
```

Alternatively, from a clean checkout, create a branch without a worktree:

```bash
rail branch create --name add-feature
```

`rail branch create` does not switch branches. Continue in the new worktree or
switch to `add-feature` in your current checkout. If `rail.toml` is missing,
run `rail install` there first.
Commit any setup changes before releasing so the primary checkout stays clean.

Make your changes and install the tools required by the project's checks. Then
publish the feature:

```bash
rail finish --title "Add feature"
```

`finish` commits pending work, rebases onto the configured integration branch,
runs checks, squashes the feature into one commit, pushes it, and creates or
reuses a pull request. Review the PR and wait for required GitHub checks to pass,
then merge it:

```bash
rail release
```

Tagging and cleanup are disabled by default. For a worktree-based task, run this
from the primary checkout to merge, tag, and remove its branch and worktree
(replace `42` with the PR number):

```bash
rail release --pr 42 --tag --delete-local-branch --delete-remote-branch --delete-worktree
```

## Automation and agents

Use explicit checkout paths and JSON results when calling Rail from scripts,
automation tools, or coding agents. For example, inspect a task checkout and
publish its completed changes:

```bash
rail inspect --path /path/to/project-feature --json
rail finish --path /path/to/project-feature --title "Add feature" --json
```

With `--json`, successful commands return an object with `version`, `ok`, and `result`.
Operation failures return `ok: false` with `error.code` and `error.message`, and
exit with a nonzero status. Use these fields and the exit status to decide the
next workflow step. Some errors report effects that already completed, such as a
successful push followed by a failed PR lookup.

Provision `rail.toml` before the run. `rail install` is a setup command and does
not support `--json`; the Python API also provides a noninteractive installation
operation. Invoke `rail release` when the PR is ready to merge. Tagging and
branch/worktree removal remain explicit options.

## Commands

- `--path`: checkout or nested directory; default: current directory.
- `--json`: machine-readable results; default: console text. Unavailable for `install`.
- `--log-format console|json`: global diagnostic format; default: `console`.
  Place it before the command, for example `rail --log-format json inspect`.

| Command                | Required                                | Optional arguments and defaults                                                                                                                      | Behavior                                                                                                                                          |
|------------------------|-----------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|
| `rail install`         | None                                    | `--path`                                                                                                                                             | Create or configure project settings.                                                                                          |
| `rail inspect`         | None                                    | `--path`, `--json`                                                                                                                                   | Read local checkout metadata. Outside Git: `workspace: null`, success. No writes or network access.                                               |
| `rail fetch`           | None                                    | `--path`, `--json`                                                                                                                                   | Fetch all branches from all Git remotes. Preserve local branches and files; no configuration needed.                                              |
| `rail branch create`   | `--name BRANCH`                         | `--path`, `--json`                                                                                                                                   | Require a clean tree; fetch the configured integration branch and create the branch without switching or moving local main.                       |
| `rail worktree create` | `--destination PATH`, `--branch BRANCH` | `--path`, `--json`                                                                                                                                   | Create a new worktree and branch from the freshly fetched configured base. Reject occupied paths and existing branches.                           |
| `rail check`           | None                                    | `--path`, `--json`, `--timeout SECONDS`: 3600 per attempt                                                                                            | Run configured commands in order with their individual retries; preserve formatter edits.                                                         |
| `rail finish`          | `--title TITLE`                         | `--path`, `--json`; `--body TEXT` or `--body-file FILE`: empty; `--timeout SECONDS`: 3600 per check                                                  | Commit pending work as `wip`, fetch/rebase, run checks, squash to TITLE, push, create/reuse PR. Return workspace, PR identity, and check attempts. |
| `rail release`         | None                                    | `--path`, `--json`; `--pr NUMBER`: current branch lookup; `--tag`, `--delete-local-branch`, `--delete-remote-branch`, `--delete-worktree`: all false | Require a clean checkout matching the PR; merge it, optionally tag its integration commit, then perform selected cleanup.                         |

`--body-file -` reads stdin; each command has `--help`.

Without `--pr`, release selects an open PR for the current branch, or a merged PR
matching its current commit.

## Configuration

Configuration is read from `rail.toml` in the repository root:

```toml
[git]
remote = "origin"
main = "main"

[checks]
commands = [
    { name = "Pre-commit", command = "pre-commit run --all-files", retries = 1 },
]

[release]
policy = "semver"
prefix = "v"
```

Every configuration field is required; unknown settings are rejected. Edit
checks directly: `retries = 0..2` allows one to three attempts; `commands = []`
skips checks. Commands run without a shell, so shell operators such as `&&`,
pipes, and redirection are not interpreted. On Windows, invoke an executable
interpreter rather than a `.bat` or `.cmd` file.

Release policies:

- `semver`: read `project.version` from the integration commit's `pyproject.toml`.
- `calver`: use `YYYY.MM.DD`, with a numeric suffix when the tag is occupied.

Versions are not incremented, and existing tags are never moved. Update the
project version in your feature changes before requesting a semver tag.

## Release behavior and recovery

- `finish` publishes the checked commit and preserves new local edits. Errors
  after publication identify completed effects; inspect the reported state
  before retrying.
- `release` checks conflicts and requested version/tag metadata before merging,
  respects GitHub merge rules, and tags the integration commit.
- PRs must have their source and target branches in the same repository. Fork
  pull requests are not supported by Rail's workflow.
- Releases use squash merges; enable **Squash and merge** in the GitHub
  repository settings. Each release adds one commit to the integration branch.
- Release creates Git tags when requested; it does not publish Python packages
  or create GitHub Release entries.

Selected cleanup steps run in this order: **update main → delete remote branch →
remove worktree → delete local branch**.

Only secondary worktrees can be removed. Retaining a worktree prevents deletion
of its branch. On Windows, run worktree removal from outside that worktree.
Remote deletion failures allow local cleanup; main update failures block local
removal; worktree removal failures block local branch deletion.

Release retries skip completed effects using Git and GitHub state. After a
worktree has been removed, retry from the surviving primary checkout with an
explicit PR number.

## Python API

[rail.api](src/rail/api.py) exposes the same operations with `Path` inputs,
returning data or raising exceptions without prompts or process exits. `install`
requires validated `InstallationSettings` and returns `(created, diff)`.

```python
from pathlib import Path

from rail.api import release

result = release(
    path=Path("/path/to/project"),
    pr=42,
    tag=True,
    delete_local_branch=True,
    delete_remote_branch=True,
    delete_worktree=True,
)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and contribution
guidelines, and [tests/README.md](tests/README.md) for test commands.

## License

Rail is distributed under the [MIT License](LICENSE).
