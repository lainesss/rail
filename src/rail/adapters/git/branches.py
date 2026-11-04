"""Read, validate, create, switch, and delete branch references."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import WorkflowError


if TYPE_CHECKING:
    from rail.adapters.git.command_runner import GitCommandRunner
    from rail.adapters.git.commits import Commits


class Branches:
    """Manage branch references and validate integration ancestry.

    :param command_runner: the Git subprocess runner for this checkout.
    :param commits: the ancestry query operations for that checkout.
    """

    def __init__(self, *, command_runner: GitCommandRunner, commits: Commits) -> None:
        self._command_runner = command_runner
        self._commits = commits

    def read_branch(self) -> str:
        """Read the current branch and reject detached HEAD.

        :return: the current local branch name.

        :raises WorkflowError: if HEAD is detached.
        """
        branch = self._command_runner.run_command(
            "branch", "--show-current"
        ).stdout.strip()
        if not branch:
            raise WorkflowError("Detached HEAD is not a feature branch.")
        return branch

    def validate_branch(self, *, name: str) -> None:
        """Validate a literal branch name without revision shorthand.

        :param name: the proposed branch name.

        :raises WorkflowError: if Git expands the supplied branch shorthand.
        """
        result = self._command_runner.run_command("check-ref-format", "--branch", name)
        if result.stdout.strip() != name:
            raise WorkflowError("Use a literal branch name, not checkout shorthand.")

    def read_branch_head(self, *, name: str) -> str | None:
        """Read an exact local branch without accepting revision expressions.

        :param name: the literal local branch name.

        :return: the commit ID, or None when the branch is absent.
        """
        return self._command_runner.read_reference(reference=f"refs/heads/{name}")

    def validate_main_update(self, *, main: str, main_commit: str) -> str | None:
        """Read local main and validate that it can advance to the selected commit.

        :param main: the validated integration branch name.
        :param main_commit: the freshly observed remote main commit.

        :return: the validated local main commit,
            or None when the local branch is absent.

        :raises WorkflowError: if local main contains unpublished or divergent commits.
        """
        local = self.read_branch_head(name=main)
        if (
            local is not None
            and local != main_commit
            and not self._commits.contains_commit(commit=local, main_commit=main_commit)
        ):
            raise WorkflowError(
                "Local main has unpublished or divergent history; update it manually."
            )
        return local

    def switch_branch(self, *, name: str) -> None:
        """Switch to an existing branch with a literal name.

        :param name: the destination branch.
        """
        self.validate_branch(name=name)
        self._command_runner.run_command(
            "switch", "--no-guess", "--", name, capture=False
        )

    def create_branch(self, *, name: str, commit: str) -> None:
        """Create a branch at the selected commit without switching or tracking.

        :param name: the validated new branch name.
        :param commit: the freshly fetched integration commit.
        """
        self._command_runner.run_command(
            "branch", "--no-track", "--", name, commit, capture=False
        )

    def delete_local_branch(self, *, name: str, expected_head: str) -> None:
        """Delete the selected local branch, accepting an already absent head.

        :param name: the feature branch name.
        :param expected_head: the merged PR's feature commit.

        :raises WorkflowError: if the local branch has another head commit.
        """
        self.validate_branch(name=name)
        head = self.read_branch_head(name=name)
        if head is None:
            return
        if head != expected_head:
            raise WorkflowError("The local feature branch has unpublished changes.")
        self._command_runner.run_command(
            "branch", "--delete", "--force", "--", name, capture=False
        )

    def delete_remote_branch(
        self, *, name: str, expected_head: str, remote: str
    ) -> None:
        """Delete the selected remote head with a lease, accepting absence.

        :param name: the feature branch name.
        :param expected_head: the merged PR's feature commit.
        :param remote: the configured publication remote.

        :raises WorkflowError: if the remote branch has another head commit.
        """
        self.validate_branch(name=name)
        result = self._command_runner.run_command(
            "ls-remote", "--heads", "--", remote, f"refs/heads/{name}"
        )
        if not result.stdout:
            return
        if result.stdout.split()[0] != expected_head:
            raise WorkflowError("The remote feature branch changed after merging.")
        self._command_runner.run_command(
            "push",
            f"--force-with-lease=refs/heads/{name}:{expected_head}",
            "--",
            remote,
            f":refs/heads/{name}",
            capture=False,
        )
