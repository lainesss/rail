"""Read and rewrite commits without selecting release policies."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rail.application.contracts import WorkflowError


if TYPE_CHECKING:
    from rail.adapters.git.command_runner import GitCommandRunner


class Commits:
    """Perform commit queries, staging, rebases, and squashes.

    :param command_runner: the Git subprocess runner for this checkout.
    """

    def __init__(self, *, command_runner: GitCommandRunner) -> None:
        self._command_runner = command_runner

    def commit_changes(self, *, title: str) -> None:
        """Stage all working-tree changes and create a commit.

        :param title: the commit subject.
        """
        self._command_runner.run_command("add", "--all", capture=False)
        self._command_runner.run_command("commit", "--message", title, capture=False)

    def rebase_onto_main(self, *, main_commit: str) -> None:
        """Rebase onto refreshed main without resolving or aborting conflicts.

        :param main_commit: the exact refreshed integration commit.
        """
        self._command_runner.run_command("rebase", main_commit, capture=False)

    def squash_changes(self, *, main_commit: str, title: str) -> None:
        """Commit feature work and check edits as one commit on refreshed main.

        :param main_commit: the refreshed integration commit used for rebase.
        :param title: the final feature commit subject.

        :raises WorkflowError: if the checked tree has no feature changes.
        """
        self._command_runner.run_command("add", "--all", capture=False)
        delta = self._command_runner.run_command(
            "diff", "--cached", "--quiet", main_commit, allowed=(0, 1)
        )
        if delta.returncode == 0:
            raise WorkflowError("The feature contains no changes to finalize.")
        self._command_runner.run_command("reset", "--soft", main_commit, capture=False)
        self._command_runner.run_command("commit", "--message", title, capture=False)

    def contains_commit(self, *, commit: str, main_commit: str) -> bool:
        """Check ancestry between two concrete commit IDs.

        :param commit: the possible ancestor.
        :param main_commit: the refreshed main tip.

        :return: whether the first commit belongs to the second's history.

        :raises WorkflowError: if the ancestor commit ID has an invalid format.
        """
        if re.fullmatch(pattern=r"[0-9a-fA-F]{40,64}", string=commit) is None:
            raise WorkflowError("Invalid integration commit ID.")
        return (
            self._command_runner.run_command(
                "merge-base", "--is-ancestor", commit, main_commit, allowed=(0, 1)
            ).returncode
            == 0
        )

    def is_squash_merge(self, *, commit: str, integration_commit: str) -> bool:
        """Verify that a single-parent commit contains the feature's merged tree.

        :param commit: the feature head whose changes must be integrated.
        :param integration_commit: the confirmed PR integration commit.

        :return: whether the integration tree matches merging the feature onto
            its sole parent, independently of later changes on main.

        :raises WorkflowError: if conflicts prevent computing the expected tree.
        """
        parents = self._command_runner.run_command(
            "rev-list", "--parents", "--max-count=1", integration_commit
        ).stdout.split()
        if len(parents) != 2:
            return False
        expected_tree = self.compute_merge_tree(
            main_commit=parents[1], feature_commit=commit
        )
        actual_tree = self._command_runner.run_command(
            "rev-parse", f"{integration_commit}^{{tree}}"
        ).stdout.strip()
        return actual_tree == expected_tree

    def compute_merge_tree(self, *, main_commit: str, feature_commit: str) -> str:
        """Compute a prospective merge tree without changing checkouts or branches.

        :param main_commit: the refreshed integration branch commit.
        :param feature_commit: the PR head to merge.

        :return: the merged tree object ID.

        :raises WorkflowError: if merge conflicts prevent a clean release.
        """
        result = self._command_runner.run_command(
            "merge-tree", "--write-tree", main_commit, feature_commit, allowed=(0, 1)
        )
        if result.returncode:
            raise WorkflowError(
                "The PR conflicts with refreshed main. Resolve the conflicts and "
                "run finish before releasing."
            )
        return result.stdout.strip()

    def read_file(self, *, commit: str, path: str) -> str:
        """Read file contents from a specific commit or merged tree.

        :param commit: the commit ID or tree ID containing the file.
        :param path: the repository-relative file path.

        :return: the file contents without interpreting their format.
        """
        return self._command_runner.run_command("show", f"{commit}:{path}").stdout

    def read_head_commit(self) -> str:
        """Read the current checkout commit.

        :return: the full commit ID.
        """
        return self._command_runner.run_command("rev-parse", "HEAD").stdout.strip()
