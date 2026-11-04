"""Exercise locking behavior with isolated repository resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from filelock import FileLock, Timeout

from tests.support.repository import create_task_checkout


if TYPE_CHECKING:
    from tests.support.repository import Project


def test_repository_lock_rejects_concurrent_mutation(*, project: Project) -> None:
    """Context: another operation holds the shared repository lock.

    Invariants:
        - A second operation fails as busy before it creates a workspace.
    """
    lock = FileLock(
        lock_file=str(project.git.read_common_directory() / "workflow.lock")
    )
    with lock.acquire(timeout=0), pytest.raises(Timeout):
        create_task_checkout(project=project)
    assert not project.workspace.exists()
