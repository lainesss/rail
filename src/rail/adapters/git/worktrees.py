"""Create, inspect, validate, and remove linked worktrees."""

from __future__ import annotations

from pathlib import Path

from rail.adapters.git.branches import Branches
from rail.adapters.git.command_runner import GitCommandRunner
from rail.adapters.git.commits import Commits
from rail.application.contracts import WorkflowError


class Worktrees:
    """Manage linked checkout registration and guarded removal.

    :param command_runner: the Git subprocess runner for the source checkout.
    """

    def __init__(self, *, command_runner: GitCommandRunner) -> None:
        self._command_runner = command_runner

    def list_checkouts(self) -> list[tuple[Path, str]]:
        """Read checkout locations and branches for safe release cleanup.

        :return: absolute checkout paths and branches, primary checkout first.
        """
        result: list[tuple[Path, str]] = []
        output = self._command_runner.run_command(
            "worktree", "list", "--porcelain", "-z"
        ).stdout
        for record in output.split("\x00\x00"):
            fields = record.split("\x00")
            path = next(
                (item[9:] for item in fields if item.startswith("worktree ")), ""
            )
            if not path or "bare" in fields:
                continue
            branch = next(
                (item[18:] for item in fields if item.startswith("branch refs/heads/")),
                "",
            )
            result.append((Path(path).resolve(), branch))
        return result

    def add_worktree(self, *, path: Path, branch: str, commit: str) -> None:
        """Create a checkout at the selected commit before attaching a new branch.
        Failed attachment removes the detached checkout only while it remains clean.

        :param path: the absolute destination directory.
        :param branch: the validated new branch name.
        :param commit: the freshly fetched integration commit.

        :raises WorkflowError: if the detached checkout cannot be removed after
            branch attachment fails.
        """
        self._command_runner.run_command(
            "worktree",
            "add",
            "--detach",
            "--",
            str(path),
            commit,
            capture=False,
        )
        checkout = GitCommandRunner(root=path)
        try:
            checkout.run_command(
                "switch", "--no-track", "--create", branch, capture=False
            )
        except WorkflowError as error:
            try:
                self.remove_worktree(path=path)
            except WorkflowError as cleanup_error:
                raise WorkflowError(
                    f"{error}\nCould not remove the detached worktree at {path}: "
                    f"{cleanup_error}"
                ) from error
            raise

    def validate_worktree_removal(self, *, path: Path, branch: str, head: str) -> None:
        """Reject changed or foreign checkouts before removal.

        :param path: the registered secondary checkout to remove.
        :param branch: the feature branch authorized for cleanup.
        :param head: the exact merged feature commit.

        :raises WorkflowError: if checkout identity or files changed.
        """
        if path.exists():
            checkout = GitCommandRunner(root=path)
            commits = Commits(command_runner=checkout)
            branches = Branches(command_runner=checkout, commits=commits)
            if (
                checkout.read_common_directory()
                != self._command_runner.read_common_directory()
                or branches.read_branch() != branch
                or commits.read_head_commit() != head
                or not checkout.is_worktree_clean()
            ):
                raise WorkflowError("The feature worktree changed; removal refused.")

    def remove_worktree(self, *, path: Path) -> None:
        """Remove a registered checkout without forcing its deletion.

        :param path: the validated secondary destination.
        """
        self._command_runner.run_command(
            "worktree", "remove", "--", str(path), capture=False
        )
