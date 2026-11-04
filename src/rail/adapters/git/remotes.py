"""Synchronize remotes and publish branches with observed leases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rail.application.contracts import WorkflowError


if TYPE_CHECKING:
    from rail.adapters.git.branches import Branches
    from rail.adapters.git.command_runner import GitCommandRunner


class Remotes:
    """Fetch remote references and retain their publication leases.

    :param command_runner: the Git subprocess runner for this checkout.
    :param branches: the literal branch-name validation operations.
    """

    def __init__(self, *, command_runner: GitCommandRunner, branches: Branches) -> None:
        self._command_runner = command_runner
        self._branches = branches
        self._heads: dict[str, dict[str, str]] = {}

    def read_remote_url(self, *, remote: str) -> str:
        """Read one remote URL and reject separate or multiple push destinations.

        :param remote: the configured authority.

        :return: the single shared fetch and push URL.

        :raises WorkflowError: if fetch and push destinations differ.
        """
        fetch = self._command_runner.run_command(
            "remote", "get-url", "--all", "--", remote
        ).stdout.splitlines()
        push = self._command_runner.run_command(
            "remote", "get-url", "--push", "--all", "--", remote
        ).stdout.splitlines()
        if len(fetch) != 1 or fetch != push:
            raise WorkflowError(
                "The configured remote must have one identical fetch/push URL."
            )
        return fetch[0]

    def fetch_remotes(self) -> None:
        """Fetch all remote branch heads without changing local branches or files."""
        for remote in self._command_runner.run_command("remote").stdout.splitlines():
            self._command_runner.run_command(
                "fetch",
                "--no-tags",
                "--",
                remote,
                f"+refs/heads/*:refs/remotes/{remote}/*",
                capture=False,
            )

    def refresh_remote(self, *, remote: str) -> None:
        """Fetch all branch heads and remember the observed publication leases.

        :param remote: the configured authority.
        """
        self.read_remote_url(remote=remote)
        self._command_runner.run_command(
            "fetch",
            "--prune",
            "--no-tags",
            "--",
            remote,
            f"+refs/heads/*:refs/remotes/{remote}/*",
            capture=False,
        )
        result = self._command_runner.run_command(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            f"refs/remotes/{remote}/",
        )
        self._heads[remote] = {}
        for line in result.stdout.splitlines():
            ref, head = line.split(sep=" ", maxsplit=1)
            self._heads[remote][ref] = head

    def read_remote_main_commit(self, *, main: str, remote: str) -> str:
        """Read remote main without moving a local branch used by another worktree.

        :param main: the configured integration branch.
        :param remote: the refreshed authority.

        :return: the exact refreshed integration commit.

        :raises WorkflowError: if the configured remote branch is absent.
        """
        self._branches.validate_branch(name=main)
        target = self._command_runner.read_reference(
            reference=f"refs/remotes/{remote}/{main}"
        )
        if target is None:
            raise WorkflowError(f"The remote branch {remote}/{main} does not exist.")
        return target

    def fetch_main_commit(self, *, main: str, remote: str) -> str:
        """Fetch the configured integration branch without changing local files.

        :param main: the configured integration branch.
        :param remote: the configured synchronization remote.

        :return: the integration commit fetched by this invocation.
        """
        self._branches.validate_branch(name=main)
        self._command_runner.run_command(
            "fetch",
            "--no-tags",
            "--write-fetch-head",
            "--",
            remote,
            f"+refs/heads/{main}:refs/remotes/{remote}/{main}",
            capture=False,
        )
        return self._command_runner.run_command(
            "rev-parse", "--verify", "FETCH_HEAD^{commit}"
        ).stdout.strip()

    def publish_branch(self, *, branch: str, remote: str, commit: str) -> None:
        """Push only the feature branch using the exact fetched lease.

        :param branch: the rewritten feature branch.
        :param remote: the configured publication authority.
        :param commit: the exact checked feature commit to publish.
        """
        expected = self._heads[remote].get(f"refs/remotes/{remote}/{branch}", "")
        self._command_runner.run_command(
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected}",
            "--set-upstream",
            "--",
            remote,
            f"{commit}:refs/heads/{branch}",
            capture=False,
        )
