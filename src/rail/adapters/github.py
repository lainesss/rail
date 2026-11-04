"""Adapt repository-scoped pull request operations to GitHub CLI."""

from __future__ import annotations

import re
from time import sleep
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    UrlConstraints,
    field_validator,
)

from rail.adapters.processes import run_process
from rail.application.contracts import NonblankText, PullRequest, WorkflowError
from rail.configuration import RuntimeConfig


if TYPE_CHECKING:
    from pathlib import Path


FIELDS = (
    "number,url,headRefName,baseRefName,state,mergeCommit,isCrossRepository,headRefOid"
)


class RepositoryUrl(BaseModel):
    """Validate a hosted repository URL before selecting its owner and name.

    :attr url: an HTTP or SSH URL identifying one owner and repository.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    url: Annotated[
        AnyUrl,
        UrlConstraints(allowed_schemes=["http", "https", "ssh"], host_required=True),
    ]

    @field_validator("url")
    @classmethod
    def validate_repository_path(cls, value: AnyUrl) -> AnyUrl:
        """Require the owner and repository segments of a validated URL.

        :param value: the structurally validated hosted URL.

        :return: the URL identifying one owner and repository.

        :raises ValueError: if the repository path has another shape.
        """
        path = (value.path or "").strip("/").removesuffix(".git")
        if len(path.split(sep="/")) != 2:
            raise ValueError("The URL must identify one owner and repository.")
        return value


def parse_repository_selector(*, url: str) -> str:
    """Extract a gh repository selector from an SSH or HTTP Git remote URL.

    :param url: the configured repository URL.

    :return: the host/owner/repository selector without URL credentials.
    """
    normalized = re.sub(
        pattern=r"^[^/@:]+@([^/:]+):", repl=r"ssh://git@\1/", string=url
    )
    repository = RepositoryUrl.model_validate(obj={"url": normalized})
    path = (repository.url.path or "").strip("/").removesuffix(".git")
    return f"{repository.url.host}/{path}"


class MergeCommit(BaseModel):
    """Validate the integration commit object returned by GitHub.

    :attr oid: the integration commit identifier.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    oid: NonblankText


class PullRequestResponse(BaseModel):
    """Build a validated PR response using GitHub's external field names.

    :attr number: the positive repository-local PR number.
    :attr url: the pull request browser URL.
    :attr head: the source branch name.
    :attr base: the target branch name.
    :attr state: the hosted pull request state.
    :attr merge: the integration commit, or None before merging.
    :attr cross_repository: whether the PR comes from another repository;
        must be false.
    :attr head_commit: the current feature commit when supplied by hosting.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    number: int = Field(gt=0)
    url: NonblankText
    head: NonblankText = Field(alias="headRefName")
    base: NonblankText = Field(alias="baseRefName")
    state: Literal["OPEN", "CLOSED", "MERGED"]
    merge: MergeCommit | None = Field(default=None, alias="mergeCommit")
    cross_repository: Literal[False] = Field(alias="isCrossRepository")
    head_commit: str = Field(default="", alias="headRefOid")

    def build_pull_request(self) -> PullRequest:
        """Convert validated hosted fields into the workflow's PR object.

        :return: the normalized pull request with an optional integration commit.
        """
        return PullRequest(
            number=self.number,
            url=self.url,
            head=self.head,
            base=self.base,
            state=self.state,
            commit="" if self.merge is None else self.merge.oid,
            head_commit=self.head_commit,
        )


class MergeResponse(BaseModel):
    """Validate confirmation of a completed GitHub merge.

    :attr merged: the required successful merge marker.
    :attr sha: the confirmed integration commit identifier.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    merged: Literal[True]
    sha: NonblankText


class GitHub:
    """Access PRs using one explicit repository selector on every invocation.

    :param root: the working directory for gh commands.
    :param url: the configured remote's repository URL.
    """

    def __init__(self, *, root: Path, url: str) -> None:
        self.root = root
        self.repository = parse_repository_selector(url=url)

    def find_pull_requests(
        self, *, branch: str, main: str, state: str
    ) -> list[PullRequest]:
        """Look up PRs while retaining enough results to reject ambiguity.

        :param branch: the feature branch filter.
        :param main: the integration branch filter.
        :param state: the open, merged, or all filter.

        :return: matching same-repository pull requests.
        """
        result = run_process(
            args=[
                "gh",
                "pr",
                "list",
                "--repo",
                self.repository,
                "--head",
                branch,
                "--base",
                main,
                "--state",
                state,
                "--limit",
                str(RuntimeConfig.PR_LOOKUP_LIMIT),
                "--json",
                FIELDS,
            ],
            cwd=self.root,
        )
        values = TypeAdapter(type=list[PullRequestResponse]).validate_json(
            result.stdout
        )
        return [value.build_pull_request() for value in values]

    def read_pull_request(
        self, *, number: int, expected_head: str | None = None
    ) -> PullRequest:
        """Read a PR, retrying a mismatched head within the configured read limit.

        :param number: the selected pull request number.
        :param expected_head: the published commit to await, or None for one read.

        :return: the selected pull request.

        :raises WorkflowError: if the expected PR head is still absent after
            the configured read limit.
        """
        remaining = RuntimeConfig.PR_HEAD_READ_ATTEMPTS - 1
        while True:
            result = run_process(
                args=[
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    self.repository,
                    "--json",
                    FIELDS,
                ],
                cwd=self.root,
            )
            selected = PullRequestResponse.model_validate_json(
                json_data=result.stdout
            ).build_pull_request()
            if expected_head is None or selected.head_commit == expected_head:
                return selected
            if remaining == 0:
                raise WorkflowError(
                    f"Push succeeded for commit {expected_head}, but PR #{number} "
                    f"({selected.url}) still reports head "
                    f"{selected.head_commit or '<missing>'} after "
                    f"{RuntimeConfig.PR_HEAD_READ_ATTEMPTS} reads. "
                    "The branch was published; inspect the PR before retrying finish."
                )
            remaining -= 1
            sleep(RuntimeConfig.PR_HEAD_RETRY_DELAY_SECONDS)

    def merge_pull_request(self, *, number: int, head_commit: str) -> PullRequest:
        """Squash-merge the expected PR head through GitHub's repository rules.

        :param number: the repository-local PR number.
        :param head_commit: the reviewed commit required for the merge.

        :return: the merged pull request and its integration commit.

        :raises WorkflowError: if the PR state or commits disagree with merge
            confirmation.
        """
        host, repository = self.repository.split(sep="/", maxsplit=1)
        result = run_process(
            args=[
                "gh",
                "api",
                "--hostname",
                host,
                "--method",
                "PUT",
                f"repos/{repository}/pulls/{number}/merge",
                "--raw-field",
                f"sha={head_commit}",
                "--raw-field",
                "merge_method=squash",
            ],
            cwd=self.root,
        )
        confirmation = MergeResponse.model_validate_json(json_data=result.stdout)
        selected = self.read_pull_request(number=number)
        if (
            selected.state != "MERGED"
            or selected.commit != confirmation.sha
            or selected.head_commit != head_commit
        ):
            raise WorkflowError(
                "GitHub has not confirmed the integration commit; retry release."
            )
        return selected

    def create_pull_request(
        self, *, branch: str, main: str, title: str, body: str
    ) -> PullRequest:
        """Create a PR with explicit inputs and no interactive fallback.

        :param branch: the published feature branch.
        :param main: the integration branch.
        :param title: the pull request title.
        :param body: the complete description, including newlines.

        :return: the created pull request and its current head.
        """
        result = run_process(
            args=[
                "gh",
                "pr",
                "create",
                "--repo",
                self.repository,
                "--head",
                branch,
                "--base",
                main,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=self.root,
        )
        number = int(urlsplit(url=result.stdout.strip()).path.rsplit("/", 1)[-1])
        return self.read_pull_request(number=number)
