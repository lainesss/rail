"""Validate GitHub CLI argument scoping and external response parsing."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from rail.adapters.github import GitHub, PullRequestResponse, parse_repository_selector
from rail.application.contracts import WorkflowError


if TYPE_CHECKING:
    from pathlib import Path


def build_pull_request_response() -> dict[str, object]:
    """Create a valid same-repository PR response.

    :return: the externally shaped pull request data.
    """
    return {
        "number": 42,
        "url": "https://github.example/owner/project/pull/42",
        "headRefName": "feature",
        "baseRefName": "trunk",
        "state": "MERGED",
        "mergeCommit": {"oid": "a" * 40},
        "isCrossRepository": False,
    }


@pytest.mark.parametrize(argnames="stale_reads", argvalues=[0, 2, 5])
def test_view_waits_for_expected_head(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_reads: int
) -> None:
    """Context: controlled gh responses initially report an older PR head.

    Invariants:
        - Matching data returns immediately or after a bounded number of rereads.
        - Five stale reads fail with the PR URL, expected SHA, and observed SHA.
        - Only read operations are retried, with one-second pauses replaced in tests.
    """
    stale = {**build_pull_request_response(), "state": "OPEN", "headRefOid": "b" * 40}
    fresh = {**stale, "headRefOid": "c" * 40}
    values = [stale] * stale_reads + [fresh]
    execute = Mock(
        side_effect=[
            subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(value))
            for value in values
        ]
    )
    pause = Mock()
    monkeypatch.setattr(target="rail.adapters.github.run_process", name=execute)
    monkeypatch.setattr(target="rail.adapters.github.sleep", name=pause)
    hosting = GitHub(root=tmp_path, url="https://github.example/owner/project")
    if stale_reads == 5:
        with pytest.raises(expected_exception=WorkflowError) as failure:
            hosting.read_pull_request(number=42, expected_head="c" * 40)
        message = str(failure.value)
        assert stale["url"] in message
        assert "b" * 40 in message and "c" * 40 in message and "5 reads" in message
    else:
        selected = hosting.read_pull_request(number=42, expected_head="c" * 40)
        assert selected.head_commit == "c" * 40 and selected.number == 42
    assert execute.call_count == min(stale_reads + 1, 5)
    assert pause.call_count == min(stale_reads, 4)
    assert all(item.args == (1,) for item in pause.call_args_list)
    assert all(
        item.kwargs["args"][:4] == ["gh", "pr", "view", "42"]
        for item in execute.call_args_list
    )


def test_merge_uses_exact_head(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: GitHub accepts merging the exact reviewed commit.

    Invariants:
        - The request uses squash, is repository-scoped, and supplies the head guard.
        - The returned PR confirms the integration commit.
    """
    value = build_pull_request_response()
    value["headRefOid"] = "c" * 40
    execute = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"merged": True, "sha": "a" * 40}),
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(value)
            ),
        ]
    )
    monkeypatch.setattr(target="rail.adapters.github.run_process", name=execute)
    hosting = GitHub(root=tmp_path, url="https://github.example/owner/project")
    selected = hosting.merge_pull_request(number=42, head_commit="c" * 40)
    assert selected.commit == "a" * 40 and selected.state == "MERGED"
    args = execute.call_args_list[0].kwargs["args"]
    assert "repos/owner/project/pulls/42/merge" in args and "sha=" + "c" * 40 in args
    assert "merge_method=squash" in args and "github.example" in args


@pytest.mark.parametrize(
    argnames="condition", argvalues=["invalid", "rejected", "commit", "head"]
)
def test_merge_requires_confirmation(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    """Context: GitHub rejects the merge or returns inconsistent confirmation.

    Invariants:
        - A merge is never reported successful without the matching PR and commit.
    """
    value = build_pull_request_response()
    value["headRefOid"] = ("d" if condition == "head" else "c") * 40
    merged = (
        []
        if condition == "invalid"
        else {
            "merged": condition != "rejected",
            "sha": ("b" if condition == "commit" else "a") * 40,
        }
    )
    execute = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(merged)
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(value)
            ),
        ]
    )
    monkeypatch.setattr(target="rail.adapters.github.run_process", name=execute)
    expected_error = (
        ValidationError if condition in {"invalid", "rejected"} else WorkflowError
    )
    with pytest.raises(expected_exception=expected_error):
        GitHub(
            root=tmp_path, url="https://github.example/owner/project"
        ).merge_pull_request(number=42, head_commit="c" * 40)


@pytest.mark.parametrize(
    argnames="url",
    argvalues=[
        "https://github.example/owner/project.git",
        "git@github.example:owner/project.git",
        "ssh://git@github.example/owner/project",
        "https://user:secret@github.example/owner/project",
    ],
)
def test_repository_selector(*, url: str) -> None:
    """Context: a configured remote uses a supported hosted Git URL.

    Invariants:
        - The selector identifies the same host and repository without credentials.
    """
    assert parse_repository_selector(url=url) == "github.example/owner/project"


@pytest.mark.parametrize(
    argnames="url",
    argvalues=[
        "/local/repository",
        "file:///tmp/project",
        "https://host/owner",
        "https://host/owner/project/extra",
        "https:///owner/project",
    ],
)
def test_invalid_repository_selector(*, url: str) -> None:
    """Context: a remote URL does not identify a supported hosted repository.

    Invariants:
        - URL validation reports unsupported repository identity.
    """
    with pytest.raises(expected_exception=ValidationError, match="url"):
        parse_repository_selector(url=url)


def test_parse_open_pr() -> None:
    """Context: a valid PR has no integration commit yet.

    Invariants:
        - The missing merge commit remains an empty identity.
    """
    value = build_pull_request_response()
    value["mergeCommit"] = None
    value["state"] = "OPEN"
    selected = PullRequestResponse.model_validate(obj=value).build_pull_request()
    assert selected.commit == ""
    assert selected.state == "OPEN"
    assert selected.number == 42


@pytest.mark.parametrize(
    argnames="key,value",
    argvalues=[
        ("number", True),
        ("number", 0),
        ("number", "42"),
        ("url", None),
        ("headRefName", ""),
        ("baseRefName", 1),
        ("state", None),
        ("isCrossRepository", True),
        ("isCrossRepository", None),
        ("mergeCommit", []),
        ("mergeCommit", {"oid": None}),
    ],
)
def test_invalid_pr_fields(*, key: str, value: object) -> None:
    """Context: a GitHub response contains malformed fields or a fork relationship.

    Invariants:
        - Boundary validation rejects the response before internal use.
    """
    data = build_pull_request_response()
    data[key] = value
    with pytest.raises(expected_exception=ValidationError):
        PullRequestResponse.model_validate(obj=data)


def test_invalid_pr_shape() -> None:
    """Context: a GitHub PR response is not an object.

    Invariants:
        - Model construction rejects non-object input.
    """
    with pytest.raises(expected_exception=ValidationError):
        PullRequestResponse.model_validate(obj=[])


def test_scoped_gh_operations(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: gh execution is replaced with controlled successful JSON responses.

    Invariants:
        - Every operation explicitly selects the configured repository.
        - Lookup includes feature, base, state, and an ambiguity-detecting limit.
        - PR creation preserves multiline descriptions without shell interpretation.
    """
    execute = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(obj=[build_pull_request_response()]),
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(obj=build_pull_request_response()),
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="https://github.example/owner/project/pull/42\n",
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(build_pull_request_response())
            ),
        ]
    )
    monkeypatch.setattr(target="rail.adapters.github.run_process", name=execute)
    hosting = GitHub(root=tmp_path, url="git@github.example:owner/project.git")
    selected = hosting.find_pull_requests(branch="feature", main="trunk", state="all")
    assert selected[0].number == 42
    assert hosting.read_pull_request(number=42) == selected[0]
    assert (
        hosting.create_pull_request(
            branch="feature", main="trunk", title="Title", body="first\nsecond"
        )
        == selected[0]
    )
    for invocation in execute.call_args_list:
        args = invocation.kwargs["args"]
        assert args[args.index("--repo") + 1] == "github.example/owner/project"
        assert invocation.kwargs["cwd"] == tmp_path
    lookup = execute.call_args_list[0].kwargs["args"]
    assert lookup[lookup.index("--limit") + 1] == "2"
    assert lookup[lookup.index("--head") + 1] == "feature"
    assert lookup[lookup.index("--base") + 1] == "trunk"
    assert execute.call_args_list[2].kwargs["args"][-1] == "first\nsecond"


def test_invalid_lookup_response(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: gh returns an object where PR lookup requires an array.

    Invariants:
        - Lookup rejects the incompatible response shape.
    """
    monkeypatch.setattr(
        target="rail.adapters.github.run_process",
        name=Mock(
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="{}")
        ),
    )
    with pytest.raises(expected_exception=ValidationError, match="list"):
        GitHub(
            root=tmp_path, url="https://github.example/owner/project"
        ).find_pull_requests(branch="feature", main="main", state="open")


def test_hosted_head_type_is_validated() -> None:
    """Context: the hosting service returns a nontextual feature revision.

    Invariants:
        - Invalid hosted revision metadata is rejected before publication evidence.
    """
    with pytest.raises(expected_exception=ValidationError, match="headRefOid"):
        PullRequestResponse.model_validate(
            obj={**build_pull_request_response(), "headRefOid": 1}
        )
