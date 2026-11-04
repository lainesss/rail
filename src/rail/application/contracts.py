"""Define workflow settings, pull request data, and errors."""

from __future__ import annotations

import shlex
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from rail.configuration import RuntimeConfig


NonblankText = Annotated[
    str, StringConstraints(strict=True, pattern=r"\A[^\x00]*[^\s\x00][^\x00]*\z")
]
Argument = Annotated[str, StringConstraints(strict=True, pattern=r"\A[^\x00]*\z")]
GitSettingStr = Annotated[
    str, StringConstraints(strict=True, pattern=r"\A[^-\s\x00][^\x00]*\z")
]


class WorkflowError(RuntimeError):
    """Report an invalid workflow state or a failed external operation."""


class OperationError(WorkflowError):
    """Identify an operation failure without requiring clients to parse its text.

    :param message: the actionable diagnostic.
    :param code: the stable error category.
    """

    def __init__(self, message: str, *, code: str = "invalid_state") -> None:
        super().__init__(message)
        self.code = code


class Check(BaseModel):
    """Store a named check with parsed arguments and its retry allowance.

    :attr name: the human-readable check label.
    :attr args: the executable and literal arguments parsed from configuration.
    :attr retries: the number of additional attempts, from zero.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: NonblankText
    args: tuple[Argument, ...] = Field(
        validation_alias=AliasChoices("args", "command"), min_length=1, strict=False
    )
    retries: int = Field(ge=0, le=RuntimeConfig.MAX_CHECK_RETRIES)

    @field_validator("args", mode="before")
    @classmethod
    def parse_arguments(cls, value: object) -> object:
        """Decode literal command text before validating its arguments.

        :param value: command text or already separated arguments.

        :return: parsed arguments for text, or the original structured input.
        """
        if isinstance(value, str):
            return shlex.split(s=value, comments=False, posix=True)
        return value

    @field_validator("args")
    @classmethod
    def validate_executable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a nonblank executable after command argument validation.

        :param value: the nonempty validated argument sequence.

        :return: the accepted literal arguments.

        :raises ValueError: if the executable is blank.
        """
        if not value[0].strip():
            raise ValueError("The command must contain a nonblank executable.")
        return value


class GitSettings(BaseModel):
    """Validate the configured remote and integration branch names.

    :attr remote: the single synchronization and publication remote.
    :attr main: the integration branch.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    remote: GitSettingStr
    main: GitSettingStr


class CheckSettings(BaseModel):
    """Validate the ordered project check configuration.

    :attr commands: the named checks with individual retry allowances.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    commands: tuple[Check, ...] = Field(strict=False)


class ReleaseSettings(BaseModel):
    """Validate the release version policy and tag prefix.

    :attr policy: the semver or calver release policy.
    :attr prefix: the optional release tag prefix.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    policy: Literal["semver", "calver"]
    prefix: Argument


class InstallationSettings(BaseModel):
    """Validate every project choice requested during installation.

    :attr git: the required repository configuration.
    :attr release: the required release configuration.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    git: GitSettings
    release: ReleaseSettings


class Settings(InstallationSettings):
    """Build complete saved settings, including the configured checks.

    :attr checks: the required check configuration read from the file.
    """

    checks: CheckSettings


class FinishInput(BaseModel):
    """Validate feature publication text before repository operations.

    :attr title: the nonblank single-line PR and commit title.
    :attr body: the literal PR description.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    title: Annotated[
        str,
        StringConstraints(pattern=r"\A[^\r\n\x00]*[^\s\r\n\x00][^\r\n\x00]*\z"),
    ]
    body: Argument


class ReleaseOptions(BaseModel):
    """Validate a release selection and its explicitly requested actions.

    :attr number: a positive PR number, or None for branch lookup.
    :attr tag: whether to publish a release tag.
    :attr delete_local_branch: whether to remove the local feature branch.
    :attr delete_remote_branch: whether to remove the remote feature branch.
    :attr delete_worktree: whether to remove the feature worktree.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    number: Annotated[int, Field(gt=0)] | None
    tag: bool
    delete_local_branch: bool
    delete_remote_branch: bool
    delete_worktree: bool


class PullRequest(BaseModel):
    """Represent a pull request resolved against the configured repository.

    :attr number: the repository-local pull request number.
    :attr url: the pull request browser URL.
    :attr head: the feature branch name.
    :attr base: the target integration branch.
    :attr state: the OPEN, CLOSED, or MERGED state.
    :attr commit: the integration commit, empty before merging.
    :attr head_commit: the current feature commit, when supplied by hosting.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    number: int = Field(gt=0)
    url: NonblankText
    head: NonblankText
    base: NonblankText
    state: Literal["OPEN", "CLOSED", "MERGED"]
    commit: str
    head_commit: str = ""
