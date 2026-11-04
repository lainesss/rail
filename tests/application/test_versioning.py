"""Exercise release metadata and tag selection directly, without repository doubles."""

from __future__ import annotations

import json
import tomllib
from datetime import date

import pytest
from pydantic import ValidationError

from rail.application.contracts import WorkflowError
from rail.application.versioning import (
    parse_release_metadata,
    select_calver_tag_name,
    select_semver_tag_name,
)


@pytest.mark.parametrize(
    argnames="version",
    argvalues=[
        "0.0.0",
        "1.2.3",
        "1.2.3-alpha.1+build.5",
        "1.2.3-01alpha",
        "1.2.3-0",
        "1.2.3+001",
    ],
)
def test_parse_semver(*, version: str) -> None:
    """Context: project metadata contains a valid static semantic version.

    Invariants:
        - Parsing retains the complete version, including prerelease and build data.
        - Unrelated project and tool metadata does not prevent release parsing.
    """
    document = (
        f'[project]\nname="example"\nversion={json.dumps(obj=version)}\n'
        "[tool.example]\nenabled=true\n"
    )
    assert parse_release_metadata(document=document).project.version == version


@pytest.mark.parametrize(
    argnames="version",
    argvalues=[
        1,
        True,
        "",
        "1.2",
        "v1.2.3",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3-",
        "1.2.3+",
        "1.2.3-rc..1",
        "1.2.3+build..1",
        "1.2.3\n",
    ],
)
def test_reject_invalid_semver(*, version: object) -> None:
    """Context: the static project version has the wrong type or SemVer syntax.

    Invariants:
        - Validation reports only the version field.
    """
    document = f"[project]\nversion={json.dumps(obj=version)}\n"
    with pytest.raises(expected_exception=ValidationError) as failure:
        parse_release_metadata(document=document)
    assert [error["loc"] for error in failure.value.errors()] == [
        ("project", "version")
    ]


def test_reject_malformed_metadata() -> None:
    """Context: the release metadata is not valid TOML.

    Invariants:
        - Parsing raises a TOML decoding error.
    """
    with pytest.raises(expected_exception=tomllib.TOMLDecodeError):
        parse_release_metadata(document="[")


@pytest.mark.parametrize(
    argnames="prefix,existing,commit,expected",
    argvalues=[
        ("v", {}, None, "v1.2.3"),
        ("", {}, "integration", "1.2.3"),
        ("release-", {"release-1.2.3": "integration"}, "integration", "release-1.2.3"),
    ],
)
def test_select_semver_tag(
    *, prefix: str, existing: dict[str, str], commit: str | None, expected: str
) -> None:
    """Context: a semantic version tag is available or already targets this release.

    Invariants:
        - Selection applies the configured prefix and reuses a matching tag.
        - Observed tag data remains unchanged.
    """
    before = existing.copy()
    assert (
        select_semver_tag_name(
            prefix=prefix, version="1.2.3", existing=existing, commit=commit
        )
        == expected
    )
    assert existing == before


@pytest.mark.parametrize(argnames="commit", argvalues=[None, "other"])
def test_reject_conflicting_semver_tag(*, commit: str | None) -> None:
    """Context: an occupied version tag cannot be confirmed as belonging to the release.

    Invariants:
        - Preflight and publication reject the conflict without changing the tag data.
    """
    existing = {"v1.2.3": "integration"}
    with pytest.raises(
        expected_exception=WorkflowError, match="another commit: v1.2.3"
    ):
        select_semver_tag_name(
            prefix="v", version="1.2.3", existing=existing, commit=commit
        )
    assert existing == {"v1.2.3": "integration"}


@pytest.mark.parametrize(
    argnames="prefix,existing,commit,expected",
    argvalues=[
        ("v", {}, None, "v2026.11.04"),
        (
            "",
            {"2026.11.04": "other", "2026.11.04.1": "other", "2026.11.04.3": "other"},
            "integration",
            "2026.11.04.2",
        ),
        ("v", {"v2026.11.03.2": "integration"}, "integration", "v2026.11.03.2"),
        ("v", {"v2026.11.04": "integration"}, None, "v2026.11.04.1"),
        ("r.", {"rX2026.11.03": "integration"}, "integration", "r.2026.11.04"),
        (
            "v",
            {"v2026.11.03.0": "integration", "v2026.11.03.01": "integration"},
            "integration",
            "v2026.11.04",
        ),
        ("v", {"v2026.11.03": "other"}, "integration", "v2026.11.04"),
    ],
    ids=[
        "new-date",
        "first-free-suffix",
        "retry-on-later-day",
        "preflight-cannot-reuse-unconfirmed-tag",
        "literal-prefix",
        "ignore-invalid-suffixes",
        "ignore-another-release",
    ],
)
def test_select_calver_tag(
    *, prefix: str, existing: dict[str, str], commit: str | None, expected: str
) -> None:
    """Context: calendar tags include collisions, prior releases, or unrelated names.

    Invariants:
        - A completed release reuses its valid tag even on a later date.
        - A new release receives the current date and first available suffix.
        - Prefix metacharacters and malformed suffixes do not match other releases.
        - Observed tag data remains unchanged.
    """
    before = existing.copy()
    assert (
        select_calver_tag_name(
            prefix=prefix,
            existing=existing,
            commit=commit,
            today=date(year=2026, month=11, day=4),
        )
        == expected
    )
    assert existing == before
