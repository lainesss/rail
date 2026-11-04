"""Run Git commands and query the local checkout context."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from rail.adapters.processes import run_process


if TYPE_CHECKING:
    import subprocess


class GitCommandRunner:
    """Run commands against one checkout with implicit Git hooks disabled.

    :param root: the working directory used for every Git invocation.
    """

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def run_command(
        self, *args: str, allowed: tuple[int, ...] = (0,), capture: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Execute Git with individually separated arguments.

        :param args: the Git subcommand and arguments.
        :param allowed: the accepted exit statuses.
        :param capture: whether stdout is query data rather than console output.

        :return: the captured command result.
        """
        return run_process(
            args=["git", "-c", f"core.hooksPath={os.devnull}", *args],
            cwd=self.root,
            allowed=allowed,
            capture=capture,
            forward_to_stderr=True,
        )

    def read_repository_root(self) -> Path:
        """Find the repository root from the current working directory.

        :return: the absolute top-level working-tree directory.
        """
        return Path(self.run_command("rev-parse", "--show-toplevel").stdout.strip())

    def read_reference(self, *, reference: str) -> str | None:
        """Read one exact reference without interpreting revision expressions.

        :param reference: the fully qualified reference name.

        :return: the referenced object ID,
            or None when that exact reference is absent.
        """
        output = self.run_command(
            "for-each-ref", "--format=%(refname) %(objectname)", reference
        ).stdout
        for line in output.splitlines():
            name, target = line.split(sep=" ", maxsplit=1)
            if name == reference:
                return target
        return None

    def read_common_directory(self) -> Path:
        """Locate shared repository metadata.

        :return: the absolute common Git directory.
        """
        return Path(
            self.run_command(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        ).resolve()

    def is_worktree_clean(self) -> bool:
        """Inspect tracked and untracked changes without refreshing the index.

        :return: whether staged, unstaged, and untracked changes are absent.
        """
        return not self.run_command(
            "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout
