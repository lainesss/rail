"""Assemble Git operations and coordinate repository-wide observations and updates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from rail.adapters.git.branches import Branches
from rail.adapters.git.command_runner import GitCommandRunner
from rail.adapters.git.commits import Commits
from rail.adapters.git.remotes import Remotes
from rail.adapters.git.tags import Tags
from rail.adapters.git.worktrees import Worktrees
from rail.application.contracts import OperationError, WorkflowError


if TYPE_CHECKING:
    from pathlib import Path


def discover_git_root(*, path: Path) -> Path | None:
    """Discover a Git root without requiring workflow configuration or a remote.

    :param path: an existing or proposed directory inside a checkout.

    :return: the containing Git root, or None outside a repository.

    :raises OperationError: if the requested path is not a directory or Git reports
        a root that does not contain it.
    """
    path = path.resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    if not path.is_dir():
        raise OperationError("The discovery path must be a directory.")
    if not any((parent / ".git").exists() for parent in (path, *path.parents)):
        return None
    root = GitCommandRunner(root=path).read_repository_root().resolve()
    if not path.is_relative_to(root):
        raise OperationError("The Git root does not contain the requested directory.")
    return root


class SourceState(BaseModel):
    """Store one read-only observation of a checkout.

    :attr branch: the checked-out branch, or None for detached HEAD.
    :attr head: the current commit, or None before the first commit.
    :attr dirty: whether tracked or untracked changes exist.
    :attr remote_url: the selected remote URL, or None when it is absent.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    branch: str | None
    head: str | None
    dirty: bool
    remote_url: str | None = None


class Git(GitCommandRunner):
    """Assemble cohesive Git operations for one checkout.

    :param root: the working directory used for every Git invocation.

    :attr branches: the branch reference operations.
    :attr commits: the commit and ancestry operations.
    :attr remotes: the synchronization and guarded publication operations.
    :attr tags: the release tag operations.
    :attr worktrees: the linked checkout operations.
    """

    def __init__(self, *, root: Path) -> None:
        super().__init__(root=root)
        self.commits = Commits(command_runner=self)
        self.branches = Branches(command_runner=self, commits=self.commits)
        self.remotes = Remotes(command_runner=self, branches=self.branches)
        self.tags = Tags(command_runner=self)
        self.worktrees = Worktrees(command_runner=self)

    def open_checkout(self, *, path: Path) -> Git:
        """Construct the operation groups for another checkout of this repository.

        :param path: the related checkout's directory.

        :return: the repository operations rooted at the selected checkout.
        """
        return Git(root=path)

    def read_source_state(self, *, remote: str) -> SourceState:
        """Read local source metadata without index refreshes or network access.

        :param remote: the remote whose configured fetch URL is requested.

        :return: the immutable observation, with unavailable values set to None.
        """
        branch = self.run_command("branch", "--show-current").stdout.strip()
        head = self.run_command("rev-parse", "--verify", "HEAD", allowed=(0, 128))
        url = self.run_command("remote", "get-url", "--", remote, allowed=(0, 2))
        return SourceState(
            branch=branch or None,
            head=head.stdout.strip() if head.returncode == 0 else None,
            dirty=not self.is_worktree_clean(),
            remote_url=url.stdout.strip() if url.returncode == 0 else None,
        )

    def update_main(self, *, main: str, remote: str) -> None:
        """Synchronize main by fast-forward only, preserving feature checkout.

        :param main: the integration branch.
        :param remote: the refreshed authority.

        :raises WorkflowError: if the main checkout has local file changes.
        """
        target = self.remotes.read_remote_main_commit(main=main, remote=remote)
        local = self.branches.validate_main_update(main=main, main_commit=target)
        if local == target:
            return
        if local is not None:
            for path, branch in self.worktrees.list_checkouts():
                if branch != main:
                    continue
                checkout = GitCommandRunner(root=path)
                if not checkout.is_worktree_clean():
                    raise WorkflowError(
                        "The main checkout must be clean before updating it."
                    )
                checkout.run_command("merge", "--ff-only", "--", target, capture=False)
                return
        self.run_command("branch", "--force", "--", main, target, capture=False)
