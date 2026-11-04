"""Interpret release metadata and select version tags without external operations."""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from rail.application.contracts import WorkflowError


if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date


_NUMERIC = r"(?:0|[1-9][0-9]*)"
_IDENTIFIER = rf"(?:{_NUMERIC}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"


class ProjectVersion(BaseModel):
    """Validate the static SemVer metadata required by the release policy.

    :attr version: the complete semantic version without a tag prefix.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    version: Annotated[
        str,
        StringConstraints(
            pattern=(
                rf"\A{_NUMERIC}\.{_NUMERIC}\.{_NUMERIC}"
                rf"(?:-{_IDENTIFIER}(?:\.{_IDENTIFIER})*)?"
                r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\z"
            )
        ),
    ]


class ReleaseMetadata(BaseModel):
    """Build validated release metadata from a decoded pyproject document.

    :attr project: the required static project version metadata.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    project: ProjectVersion


def parse_release_metadata(*, document: str) -> ReleaseMetadata:
    """Decode project TOML and build its validated release metadata.

    :param document: the target project's metadata document.

    :return: the validated metadata object.
    """
    metadata = tomllib.loads(document)
    return ReleaseMetadata.model_validate(obj=metadata)


def select_semver_tag_name(
    *, prefix: str, version: str, existing: Mapping[str, str], commit: str | None
) -> str:
    """Select a semantic version tag and reject a conflicting existing target.

    :param prefix: the configured tag prefix.
    :param version: the validated semantic version.
    :param existing: observed tag names and their commit IDs.
    :param commit: the integration commit, or None before merging.

    :return: the prefixed semantic version tag name.

    :raises WorkflowError: if an existing semantic version tag has another target.
    """
    name = prefix + version
    if name in existing and existing[name] != commit:
        raise WorkflowError(f"Release tag points to another commit: {name}")
    return name


def select_calver_tag_name(
    *, prefix: str, existing: Mapping[str, str], commit: str | None, today: date
) -> str:
    """Reuse a completed calendar version tag or select today's first available name.

    :param prefix: the configured tag prefix.
    :param existing: observed tag names and their commit IDs.
    :param commit: the integration commit, or None before merging.
    :param today: the local calendar date for a new calver tag.

    :return: a matching existing tag or an available date-based tag name.
    """
    base = prefix + today.strftime("%Y.%m.%d")
    pattern = re.escape(pattern=prefix) + r"\d{4}\.\d{2}\.\d{2}(?:\.[1-9][0-9]*)?"
    completed = sorted(
        name
        for name, target in existing.items()
        if target == commit and re.fullmatch(pattern=pattern, string=name)
    )
    if completed:
        return completed[0]
    name = base
    suffix = 0
    while name in existing:
        suffix += 1
        name = f"{base}.{suffix}"
    return name
