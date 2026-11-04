"""Share isolated repositories and task checkout creation across scenarios."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from rail.adapters.git.repository import Git
from rail.commands import create_worktree


class Project(BaseModel):
    """Retain a real repository and isolated hosting observations.

    :attr git: the source repository with a bare local remote.
    :attr workspace: the requested task worktree destination.
    :attr created: PR titles requested through the controlled hosting boundary.
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        validate_assignment=True,
    )

    git: Git
    workspace: Path
    created: list[str] = Field(default_factory=list)


def create_task_checkout(*, project: Project) -> Git:
    """Create a new task worktree for an isolated scenario.

    :param project: the configured source repository fixture.

    :return: the newly created task checkout.
    """
    create_worktree(
        path=project.git.root, destination=project.workspace, branch="codex/task-17"
    )
    return Git(root=project.workspace)
