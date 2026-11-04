"""Exercise Git effects against disposable working trees and bare remotes."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from rail.adapters.git.repository import Git
from rail.adapters.github import GitHub
from rail.adapters.processes import run_process
from rail.application.contracts import (
    Check,
    FinishInput,
    PullRequest,
    ReleaseOptions,
    WorkflowError,
)
from rail.application.workflow.finish import publish_feature
from rail.application.workflow.release import (
    release_pull_request,
    select_release_tag_name,
)
from rail.application.workspaces import create_branch
from tests.support.configuration import make_settings


if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    argnames="state", argvalues=["files", "branch", "head", "foreign"]
)
def test_worktree_removal_validates_checkout_identity(
    *,
    git_project: Git,
    state: str,
    tmp_path: Path,
) -> None:
    """Context: removal validation receives changed files or a different checkout.

    Invariants:
        - Invalid identity or unfinished files prevent removal.
        - The existing checkout and its tracked contents remain present.
    """
    path = git_project.root
    branch = "wrong" if state == "branch" else "main"
    head = "f" * 40 if state == "head" else git_project.commits.read_head_commit()
    if state == "files":
        (path / "unfinished.txt").write_text(data="keep", encoding="utf-8")
    if state == "foreign":
        other = tmp_path / "foreign"
        other.mkdir()
        Git(root=other).run_command("init")
        path = other
    with pytest.raises(
        expected_exception=WorkflowError, match="changed; removal refused"
    ):
        git_project.worktrees.validate_worktree_removal(
            path=path, branch=branch, head=head
        )
    assert path.exists()
    assert (git_project.root / "content.txt").read_text(encoding="utf-8") == "base\n"


def test_complete_lifecycle(*, git_project: Git) -> None:
    """Context: a feature is finalized, squash-merged, and released after main advances.

    Invariants:
        - Finalization publishes one feature commit with the title as its subject.
        - Successful check-produced changes belong to that same clean commit.
        - The annotated release tag targets the integration commit, not newer main.
        - Local and remote cleanup accept already absent feature branches.
    """
    git = git_project
    assert git.read_repository_root() == git.root
    create_branch(repository=git, settings=make_settings(), name="feature/example")
    git.branches.switch_branch(name="feature/example")
    (git.root / "content.txt").write_text(data="feature\n", encoding="utf-8")
    git.commits.commit_changes(title="First")
    (git.root / "other.txt").write_text(data="uncommitted\n", encoding="utf-8")
    hosting = Mock(spec=GitHub)
    hosting.find_pull_requests.return_value = []
    hosting.create_pull_request.return_value = PullRequest(
        number=1,
        url="https://example.test/pr/1",
        head="feature/example",
        base="main",
        state="OPEN",
        commit="",
    )
    hosting.read_pull_request.side_effect = lambda *, number, expected_head: (
        PullRequest.model_validate(
            {
                **hosting.create_pull_request.return_value.model_dump(),
                "number": number,
                "head_commit": expected_head,
            }
        )
    )

    def apply_check_fix(command: Check) -> None:
        """Represent a project check that successfully modifies a file.

        :param command: the configured command received by the check boundary.
        """
        assert command.args == ("fix",)
        (git.root / "fixed.txt").write_text(data="fixed\n", encoding="utf-8")

    assert publish_feature(
        repository=git,
        hosting=hosting,
        settings=make_settings(checks=(Check(name="Fixer", args=("fix",), retries=0),)),
        request=FinishInput(title="Feature title", body="Description"),
        execute=apply_check_fix,
    )[0] == PullRequest.model_validate(
        {
            **hosting.create_pull_request.return_value.model_dump(),
            "head_commit": git.commits.read_head_commit(),
        }
    )
    assert git.is_worktree_clean()
    assert git.run_command("rev-list", "--count", "main..HEAD").stdout.strip() == "1"
    assert git.run_command("log", "-1", "--format=%s").stdout.strip() == "Feature title"
    assert git.run_command("show", "HEAD:fixed.txt").stdout.splitlines() == ["fixed"]
    remote_feature = git.run_command(
        "rev-parse", "refs/remotes/origin/feature/example"
    ).stdout.strip()
    assert remote_feature == git.run_command("rev-parse", "HEAD").stdout.strip()
    # Reusing a published branch also exercises a nonempty explicit force lease.
    hosting.find_pull_requests.return_value = [
        PullRequest(
            number=1,
            url="https://example.test/pr/1",
            head="feature/example",
            base="main",
            state="OPEN",
            commit="",
        )
    ]
    assert publish_feature(
        repository=git,
        hosting=hosting,
        settings=make_settings(),
        request=FinishInput(title="Feature title", body=""),
        execute=Mock(),
    )[0] == PullRequest.model_validate(
        {
            **hosting.create_pull_request.return_value.model_dump(),
            "head_commit": git.commits.read_head_commit(),
        }
    )
    git.branches.switch_branch(name="main")
    remote_feature = git.branches.read_branch_head(name="feature/example")
    assert remote_feature is not None
    git.run_command("merge", "--squash", "feature/example")
    git.run_command("commit", "--message", "Integrated feature")
    integrated = git.run_command("rev-parse", "HEAD").stdout.strip()
    (git.root / "pyproject.toml").write_text(
        data='[project]\nversion = "9.9.9"\n', encoding="utf-8"
    )
    git.commits.commit_changes(title="Later work")
    git.run_command("push", "origin", "main")
    hosting.read_pull_request.side_effect = None
    hosting.read_pull_request.return_value = PullRequest(
        number=1,
        url="https://example.test/pr/1",
        head="feature/example",
        base="main",
        state="MERGED",
        commit=integrated,
        head_commit=remote_feature,
    )
    tag, _ = release_pull_request(
        repository=git,
        hosting=hosting,
        settings=make_settings(),
        options=ReleaseOptions(
            number=1,
            tag=True,
            delete_local_branch=False,
            delete_remote_branch=False,
            delete_worktree=False,
        ),
        today=date(year=2026, month=10, day=24),
    )
    assert tag == "v1.2.3"
    assert git.run_command("rev-parse", "v1.2.3^{commit}").stdout.strip() == integrated
    assert git.run_command("cat-file", "-t", "v1.2.3").stdout.strip() == "tag"
    assert (
        git.run_command(
            "for-each-ref", "--format=%(contents:subject)", "refs/tags/v1.2.3"
        ).stdout.strip()
        == tag
    )
    assert "refs/tags/v1.2.3" in git.run_command("ls-remote", "--tags", "origin").stdout
    git.branches.delete_local_branch(
        name="feature/example", expected_head=remote_feature
    )
    git.branches.delete_local_branch(
        name="feature/example", expected_head=remote_feature
    )
    git.branches.delete_remote_branch(
        name="feature/example", remote="origin", expected_head=remote_feature
    )
    git.branches.delete_remote_branch(
        name="feature/example", remote="origin", expected_head=remote_feature
    )
    assert git.branches.read_branch_head(name="feature/example") is None
    assert not git.run_command(
        "ls-remote", "--heads", "origin", "refs/heads/feature/example"
    ).stdout


def test_finish_preserves_fixer_changes_after_two_retries(*, git_project: Git) -> None:
    """Context: a project fixer edits files and succeeds only on its third execution.

    Invariants:
        - The configured command runs at most three times with two retries.
        - Successful fixer changes belong to the single published feature commit.
        - The working tree is clean after finalization.
    """
    import sys

    git = git_project
    create_branch(repository=git, settings=make_settings(), name="feature/fixer")
    git.branches.switch_branch(name="feature/fixer")
    (git.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    code = (
        "from pathlib import Path; p=Path('fixed.txt'); "
        "attempt=int(p.read_text())+1 if p.exists() else 1; "
        "p.write_text(str(attempt)); raise SystemExit(attempt < 3)"
    )
    fixer = Check(name="Fixer", args=(sys.executable, "-c", code), retries=2)

    def run_check(command: Check) -> None:
        """Execute the configured fixer through the actual process boundary.

        :param command: the named check and its literal arguments.
        """
        run_process(args=command.args, cwd=git.root, capture=False, timeout=None)

    hosting = Mock(spec=GitHub)
    hosting.find_pull_requests.return_value = []
    hosting.create_pull_request.return_value = PullRequest(
        number=1,
        url="https://example.test/pr/1",
        head="feature/fixer",
        base="main",
        state="OPEN",
        commit="",
    )
    hosting.read_pull_request.side_effect = lambda *, number, expected_head: (
        PullRequest.model_validate(
            {
                **hosting.create_pull_request.return_value.model_dump(),
                "number": number,
                "head_commit": expected_head,
            }
        )
    )
    assert publish_feature(
        repository=git,
        hosting=hosting,
        settings=make_settings(checks=(fixer,)),
        request=FinishInput(title="Fixed feature", body=""),
        execute=run_check,
    )[0] == PullRequest.model_validate(
        {
            **hosting.create_pull_request.return_value.model_dump(),
            "head_commit": git.commits.read_head_commit(),
        }
    )
    assert (git.root / "fixed.txt").read_text(encoding="utf-8") == "3"
    assert git.run_command("show", "HEAD:fixed.txt").stdout == "3"
    assert git.run_command("rev-list", "--count", "main..HEAD").stdout.strip() == "1"
    assert git.is_worktree_clean()


def test_safe_main_updates(*, git_project: Git) -> None:
    """Context: local main is behind remote, first checked out and then absent.

    Invariants:
        - Main advances without creating merge commits or moving a feature checkout.
        - An absent local main is recreated at the remote commit.
    """
    git = git_project
    initial = git.run_command("rev-parse", "HEAD").stdout.strip()
    (git.root / "new.txt").write_text(data="remote work", encoding="utf-8")
    git.commits.commit_changes(title="Advance")
    target = git.run_command("rev-parse", "HEAD").stdout.strip()
    git.run_command("push", "origin", "main")
    git.run_command("reset", "--hard", initial)
    git.remotes.refresh_remote(remote="origin")
    git.update_main(main="main", remote="origin")
    assert git.run_command("rev-parse", "HEAD").stdout.strip() == target
    git.run_command("switch", "--no-guess", "--create", "feature")
    git.run_command("branch", "--force", "main", initial)
    git.update_main(main="main", remote="origin")
    assert git.branches.read_branch_head(name="main") == target
    assert git.branches.read_branch() == "feature"
    git.run_command("branch", "--delete", "--force", "main")
    git.update_main(main="main", remote="origin")
    assert git.run_command("rev-parse", "main").stdout.strip() == target


def test_reject_unsafe_main(*, git_project: Git) -> None:
    """Context: local main contains unpublished work.

    Invariants:
        - Synchronization rejects the state and preserves the local commit.
    """
    git = git_project
    (git.root / "local.txt").write_text(data="local", encoding="utf-8")
    git.commits.commit_changes(title="Unpublished")
    before = git.run_command("rev-parse", "HEAD").stdout
    git.remotes.refresh_remote(remote="origin")
    with pytest.raises(expected_exception=WorkflowError, match="unpublished"):
        git.update_main(main="main", remote="origin")
    assert git.run_command("rev-parse", "HEAD").stdout == before


@pytest.mark.parametrize(
    argnames="kind", argvalues=["content", "feature-delete", "main-delete"]
)
def test_rebase_leaves_conflicts(*, git_project: Git, kind: str) -> None:
    """Context: feature and main contain conflicting edits or modify/delete changes.

    Invariants:
        - Rebase fails and leaves conflicting paths unresolved.
        - Non-conflicting main additions remain in the checkout.
    """
    git = git_project
    git.run_command("switch", "--no-guess", "--create", "feature")
    if kind == "feature-delete":
        (git.root / "content.txt").unlink()
    else:
        (git.root / "content.txt").write_text(
            data="feature version\n", encoding="utf-8"
        )
    git.commits.commit_changes(title="Feature")
    git.branches.switch_branch(name="main")
    if kind == "main-delete":
        (git.root / "content.txt").unlink()
    else:
        (git.root / "content.txt").write_text(data="main version\n", encoding="utf-8")
    (git.root / "main-only.txt").write_text(data="preserved\n", encoding="utf-8")
    git.commits.commit_changes(title="Main")
    target = git.run_command("rev-parse", "HEAD").stdout.strip()
    git.branches.switch_branch(name="feature")
    with pytest.raises(expected_exception=WorkflowError):
        git.commits.rebase_onto_main(main_commit=target)
    assert (
        "content.txt"
        in git.run_command("diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    )
    assert (git.root / "main-only.txt").read_text(encoding="utf-8") == "preserved\n"


@pytest.mark.parametrize(
    argnames="mode", argvalues=["squash", "merge", "root", "unrelated"]
)
def test_squash_integration_verifies_feature_changes(
    *, git_project: Git, mode: str
) -> None:
    """Context: an integration commit may squash, merge, or omit the feature.

    Invariants:
        - Only a single-parent commit with the expected merged tree is accepted.
        - Verification remains valid after main advances with unrelated work.
    """
    git = git_project
    git.run_command("switch", "--create", "feature")
    (git.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    git.commits.commit_changes(title="Feature")
    feature = git.commits.read_head_commit()
    git.branches.switch_branch(name="main")
    if mode == "squash":
        git.run_command("merge", "--squash", "feature")
        git.commits.commit_changes(title="Integrated feature")
    elif mode == "merge":
        git.run_command("merge", "--no-ff", "--message", "Merge feature", "feature")
    elif mode == "unrelated":
        (git.root / "unrelated.txt").write_text(data="other", encoding="utf-8")
        git.commits.commit_changes(title="Unrelated work")
    integrated = git.commits.read_head_commit()
    (git.root / "later.txt").write_text(data="later", encoding="utf-8")
    git.commits.commit_changes(title="Later work")
    accepted = git.commits.is_squash_merge(
        commit=feature, integration_commit=integrated
    )
    assert accepted == (mode == "squash")


def test_squash_integration_rejects_new_feature_changes(*, git_project: Git) -> None:
    """Context: the feature branch gains another change after its squash merge.

    Invariants:
        - The old integration commit does not authorize deleting the new changes.
    """
    git = git_project
    git.run_command("switch", "--create", "feature")
    (git.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    git.commits.commit_changes(title="Feature")
    git.branches.switch_branch(name="main")
    git.run_command("merge", "--squash", "feature")
    git.commits.commit_changes(title="Integrated feature")
    integrated = git.commits.read_head_commit()
    git.branches.switch_branch(name="feature")
    (git.root / "unfinished.txt").write_text(data="keep", encoding="utf-8")
    git.commits.commit_changes(title="Unmerged work")
    assert not git.commits.is_squash_merge(
        commit=git.commits.read_head_commit(), integration_commit=integrated
    )


def test_git_rejections(*, git_project: Git) -> None:
    """Context: Git encounters invalid branch, commit, and remote states.

    Invariants:
        - Invalid operations fail with specific workflow diagnostics.
    """
    git = git_project
    target = git.run_command("rev-parse", "HEAD").stdout.strip()
    with pytest.raises(expected_exception=WorkflowError, match="no changes"):
        git.commits.squash_changes(main_commit=target, title="Empty")
    with pytest.raises(expected_exception=WorkflowError, match="commit ID"):
        git.commits.contains_commit(commit="--bad", main_commit=target)
    git.run_command("switch", "--detach")
    with pytest.raises(expected_exception=WorkflowError, match="Detached"):
        git.branches.read_branch()
    git.run_command(
        "remote", "set-url", "--push", "origin", "https://example.test/other/repo"
    )
    with pytest.raises(expected_exception=WorkflowError, match="fetch/push"):
        git.remotes.read_remote_url(remote="origin")


@pytest.mark.parametrize(
    argnames="document", argvalues=["[project]\nname='example'\n", "project=1\n"]
)
def test_version_requires_static_metadata(*, git_project: Git, document: str) -> None:
    """Context: a release target lacks a static project version.

    Invariants:
        - Version lookup reports the unsupported metadata.
    """
    git = git_project
    (git.root / "pyproject.toml").write_text(data=document, encoding="utf-8")
    git.commits.commit_changes(title="Metadata")
    with pytest.raises(expected_exception=ValidationError, match="project"):
        select_release_tag_name(
            repository=git,
            settings=make_settings(),
            version_ref="HEAD",
            commit=None,
            today=date(year=2026, month=10, day=24),
        )


def test_remote_only_tags(*, git_project: Git) -> None:
    """Context: a pushed tag is removed from local refs.

    Invariants:
        - Fresh remote tag lookup still reports the occupied name.
    """
    git = git_project
    git.tags.publish_tag(name="v1.2.3", commit="HEAD", remote="origin")
    git.run_command("tag", "--delete", "v1.2.3")
    assert "v1.2.3" in git.tags.read_tags(remote="origin")


def test_reference_lookup_does_not_match_descendants(*, git_project: Git) -> None:
    """Context: a missing branch name is also the prefix of an existing branch.

    Invariants:
        - Exact lookup returns no commit for the absent parent branch.
        - Lookup still returns the correct commit for the nested branch.
    """
    head = git_project.commits.read_head_commit()
    git_project.run_command("branch", "feature/nested", head)
    assert git_project.read_reference(reference="refs/heads/feature") is None
    assert git_project.read_reference(reference="refs/heads/feature/nested") == head


def test_missing_remote_main_is_rejected(*, git_project: Git) -> None:
    """Context: the configured integration branch is absent from the fetched remote.

    Invariants:
        - Lookup reports the exact missing remote branch.
        - The existing main branch and source checkout remain unchanged.
    """
    head = git_project.commits.read_head_commit()
    with pytest.raises(
        expected_exception=WorkflowError, match="remote branch origin/missing"
    ):
        git_project.remotes.read_remote_main_commit(main="missing", remote="origin")
    assert git_project.branches.read_branch() == "main"
    assert git_project.commits.read_head_commit() == head


def test_release_tag_retry_and_conflicting_targets(*, git_project: Git) -> None:
    """Context: a tag push is resumed from a local tag, then repeated after publication.

    Invariants:
        - A matching tag is published once and reused without replacement.
        - Conflicting local or remote targets are preserved and rejected.
    """
    git = git_project
    head = git.run_command("rev-parse", "HEAD").stdout.strip()
    git.run_command("tag", "--annotate", "--message", "release", "v1", head)
    git.tags.publish_tag(name="v1", commit=head, remote="origin")
    git.tags.publish_tag(name="v1", commit=head, remote="origin")
    git.run_command("tag", "light", head)
    git.run_command("push", "origin", "refs/tags/light")
    assert git.tags.read_tags(remote="origin")["light"] == head
    (git.root / "next.txt").write_text(data="next", encoding="utf-8")
    git.commits.commit_changes(title="Next")
    next_head = git.run_command("rev-parse", "HEAD").stdout.strip()
    with pytest.raises(expected_exception=WorkflowError, match="another commit"):
        git.tags.publish_tag(name="v1", commit=next_head, remote="origin")
    git.run_command("tag", "local", head)
    with pytest.raises(expected_exception=WorkflowError, match="another commit"):
        git.tags.publish_tag(name="local", commit=next_head, remote="origin")
    assert git.run_command("rev-parse", "v1^{commit}").stdout.strip() == head


def test_cleanup_rejects_changed_branch_heads(*, git_project: Git) -> None:
    """Context: a completed feature's local or remote branch has gained new commits.

    Invariants:
        - Cleanup refuses either changed head and preserves both references.
    """
    git = git_project
    initial = git.run_command("rev-parse", "HEAD").stdout.strip()
    git.run_command("switch", "--no-guess", "--create", "feature")
    (git.root / "feature.txt").write_text(data="new work", encoding="utf-8")
    git.commits.commit_changes(title="New work")
    head = git.run_command("rev-parse", "HEAD").stdout.strip()
    git.run_command("push", "origin", "feature")
    git.branches.switch_branch(name="main")
    for remote in (None, "origin"):
        with pytest.raises(
            expected_exception=WorkflowError, match="changed|unpublished"
        ):
            if remote is None:
                git.branches.delete_local_branch(name="feature", expected_head=initial)
            else:
                git.branches.delete_remote_branch(
                    name="feature", expected_head=initial, remote=remote
                )
    assert git.branches.read_branch_head(name="feature") == head
    assert (
        head
        in git.run_command(
            "ls-remote", "--heads", "origin", "refs/heads/feature"
        ).stdout
    )


def test_main_update_preserves_dirty_other_checkout(*, git_project: Git) -> None:
    """Context: main is behind remote and dirty in another registered checkout.

    Invariants:
        - Updating main from the feature checkout refuses to alter dirty main.
    """
    git = git_project
    initial = git.run_command("rev-parse", "HEAD").stdout.strip()
    (git.root / "main.txt").write_text(data="main", encoding="utf-8")
    git.commits.commit_changes(title="Advance")
    git.run_command("push", "origin", "main")
    git.run_command("reset", "--hard", initial)
    task = git.root.parent / "task"
    git.run_command("worktree", "add", "-b", "feature", str(task))
    (git.root / "dirty.txt").write_text(data="preserve", encoding="utf-8")
    feature = Git(root=task)
    feature.remotes.refresh_remote(remote="origin")
    with pytest.raises(expected_exception=WorkflowError, match="clean"):
        feature.update_main(main="main", remote="origin")
    assert git.run_command("rev-parse", "HEAD").stdout.strip() == initial
    assert (git.root / "dirty.txt").read_text(encoding="utf-8") == "preserve"


def test_literal_branch_names(*, git_project: Git) -> None:
    """Context: checkout shorthand refers to the previous branch.

    Invariants:
        - The shorthand is rejected rather than expanded into a different branch.
    """
    git_project.run_command("switch", "--no-guess", "--create", "feature")
    with pytest.raises(expected_exception=WorkflowError, match="literal"):
        git_project.branches.validate_branch(name="@{-1}")


def test_rebase_failure_without_conflicts(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context: the Git command boundary raises a rebase failure.

    Invariants:
        - The failure propagates without continuation or automatic abort.
    """
    command = Mock(side_effect=WorkflowError("Rebase failed"))
    git = Git(root=tmp_path)
    monkeypatch.setattr(target=git, name="run_command", value=command)
    with pytest.raises(expected_exception=WorkflowError, match="Rebase failed"):
        git.commits.rebase_onto_main(main_commit="c" * 40)
    command.assert_called_once_with("rebase", "c" * 40, capture=False)


def test_publish_uses_exact_checked_commit(*, git_project: Git) -> None:
    """Context: local HEAD advances after a feature commit has been selected to publish.

    Invariants:
        - Only the selected checked commit reaches the remote feature branch.
        - The newer local commit remains unchanged in the source checkout.
    """
    git = git_project
    git.branches.switch_branch(name="main")
    git.run_command("switch", "--create", "feature")
    (git.root / "feature.txt").write_text(data="checked", encoding="utf-8")
    git.commits.commit_changes(title="Checked feature")
    checked = git.commits.read_head_commit()
    (git.root / "feature.txt").write_text(data="later edit", encoding="utf-8")
    git.commits.commit_changes(title="Later unchecked commit")
    newer = git.commits.read_head_commit()
    git.remotes.refresh_remote(remote="origin")
    git.remotes.publish_branch(branch="feature", remote="origin", commit=checked)
    assert git.run_command(
        "ls-remote", "--heads", "origin", "refs/heads/feature"
    ).stdout.startswith(checked)
    assert newer != checked and git.commits.read_head_commit() == newer


def test_force_lease_rejects_changed_remote(
    *, git_project: Git, capfd: pytest.CaptureFixture[str]
) -> None:
    """Context: the remote feature changes after its publication lease is fetched.

    Invariants:
        - Publication fails and preserves the unexpected remote reference.
    """
    git = git_project
    initial = git.run_command("rev-parse", "HEAD").stdout.strip()
    git.run_command("switch", "--no-guess", "--create", "feature")
    (git.root / "feature.txt").write_text(data="feature", encoding="utf-8")
    git.commits.commit_changes(title="Feature")
    git.run_command("push", "origin", "feature")
    git.remotes.refresh_remote(remote="origin")
    git.run_command("push", "--force", "origin", f"{initial}:refs/heads/feature")
    with pytest.raises(expected_exception=WorkflowError, match="force-with-lease"):
        git.remotes.publish_branch(
            branch="feature", remote="origin", commit=git.commits.read_head_commit()
        )
    assert "stale info" in capfd.readouterr().err
    assert git.run_command(
        "ls-remote", "--heads", "origin", "refs/heads/feature"
    ).stdout.startswith(initial)
