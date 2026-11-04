"""Create repository-wide locks shared by command entry points."""

from __future__ import annotations

from typing import TYPE_CHECKING

from filelock import FileLock


if TYPE_CHECKING:
    from rail.adapters.git.repository import Git


def create_operation_lock(*, repository: Git) -> FileLock:
    """Build the shared native mutex used by all repository command entry points.

    :param repository: any checkout in the shared repository.

    :return: the operation lock, which the caller must acquire and release.
    """
    return FileLock(lock_file=str(repository.read_common_directory() / "workflow.lock"))
