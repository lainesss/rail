"""Execute external programs without a shell and bound captured diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import ExitStack, suppress
from pathlib import Path
from tempfile import TemporaryFile
from typing import TYPE_CHECKING

import psutil

from rail.application.contracts import WorkflowError
from rail.configuration import RuntimeConfig


if TYPE_CHECKING:
    from collections.abc import Sequence


class CommandError(WorkflowError):
    """Report an external execution failure separately from workflow input rejection."""


def stop_process_tree(*, child: subprocess.Popen[bytes]) -> None:
    """Stop the owned program and its observed descendants before returning.

    :param child: the still-owned direct process.

    :raises CommandError: if all descendants cannot be stopped and reaped.
    """
    descendants: list[psutil.Process] = []
    try:
        with suppress(psutil.NoSuchProcess):
            descendants = psutil.Process(pid=child.pid).children(recursive=True)
        for process in reversed(descendants):
            with suppress(psutil.NoSuchProcess):
                process.kill()
    except psutil.Error as error:
        raise CommandError("Cannot stop all command descendants.") from error
    finally:
        child.kill()
        child.wait(timeout=RuntimeConfig.CHILD_STOP_TIMEOUT_SECONDS)
    _, alive = psutil.wait_procs(
        procs=descendants, timeout=RuntimeConfig.DESCENDANT_STOP_TIMEOUT_SECONDS
    )
    if alive:
        raise CommandError("Command descendants did not stop.")


def run_process(
    *,
    args: Sequence[str],
    cwd: Path,
    capture: bool = True,
    allowed: tuple[int, ...] = (0,),
    timeout: float | None = RuntimeConfig.QUERY_TIMEOUT_SECONDS,
    check_status: bool = True,
    project_environment: bool = False,
    forward_to_stderr: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a program with explicit arguments, bounded queries, and cancellation.
    Queries use temporary streams; command output can go directly to the console.
    Timeout and cancellation terminate the owned child and observed descendants.

    :param args: the executable and its individual arguments.
    :param cwd: the working directory.
    :param capture: whether to capture both streams instead of forwarding them.
    :param allowed: the accepted process exit statuses.
    :param timeout: the seconds to wait, or None for cancellable long-running checks.
    :param check_status: whether unaccepted exit statuses raise immediately.
    :param project_environment: whether configured checks use the checkout's .venv.
    :param forward_to_stderr: whether forwarded stdout uses stderr to preserve JSON.

    :return: the completed status and captured text, or empty text when forwarded.

    :raises WorkflowError: if the resolved executable is a .bat or .cmd file.
    :raises CommandError: if the executable is missing, startup fails, execution
        times out, captured output exceeds the limit, or the exit status is rejected.
    """
    environment = dict(os.environ)
    scripts = cwd / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    if project_environment and scripts.is_dir():
        environment["PATH"] = str(scripts) + os.pathsep + environment.get("PATH", "")
        environment["VIRTUAL_ENV"] = str(scripts.parent)
    executable = shutil.which(cmd=args[0], path=environment.get("PATH"))
    if executable is None:
        raise CommandError(f"Executable not found: {args[0]}")
    if Path(executable).suffix.lower() in {".bat", ".cmd"}:
        raise WorkflowError(
            "Use an executable interpreter instead of a .bat/.cmd file."
        )
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        environment.pop(name, None)
    environment.update(
        GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", GIT_EDITOR="true"
    )
    with ExitStack() as streams:
        output = streams.enter_context(TemporaryFile(mode="w+b")) if capture else None
        errors = streams.enter_context(TemporaryFile(mode="w+b")) if capture else None
        try:
            child = subprocess.Popen(
                args=[executable, *args[1:]],
                cwd=cwd,
                env=environment,
                stdout=output if capture else sys.stderr if forward_to_stderr else None,
                stderr=errors if capture else None,
                stdin=subprocess.DEVNULL,
            )
            try:
                code = child.wait(timeout=timeout)
            except BaseException:
                stop_process_tree(child=child)
                raise
        except subprocess.TimeoutExpired as error:
            raise CommandError(
                f"{args[0]} timed out after {timeout} seconds."
            ) from error
        except OSError as error:
            raise CommandError(
                f"Unable to start {args[0]}: {error.strerror}"
            ) from error
        if output is not None and errors is not None:
            output.seek(0)
            errors.seek(0)
            output_bytes = output.read(RuntimeConfig.CAPTURE_LIMIT_BYTES + 1)
            error_bytes = errors.read(RuntimeConfig.CAPTURE_LIMIT_BYTES + 1)
        else:
            output_bytes = error_bytes = b""
    exceeded = (
        len(output_bytes) > RuntimeConfig.CAPTURE_LIMIT_BYTES
        or len(error_bytes) > RuntimeConfig.CAPTURE_LIMIT_BYTES
    )
    if exceeded:
        raise CommandError(
            f"{args[0]} output exceeded {RuntimeConfig.CAPTURE_LIMIT_BYTES:,} bytes; "
            "query rejected."
        )
    stdout = output_bytes.decode(encoding="utf-8", errors="replace")
    stderr = error_bytes.decode(encoding="utf-8", errors="replace")
    if check_status and code not in allowed:
        command = " ".join(args)
        detail = stderr.strip() if capture else "See forwarded command output."
        raise CommandError(f"Command failed ({code}): {command}\n{detail}")
    return subprocess.CompletedProcess(
        args=list(args), returncode=code, stdout=stdout, stderr=stderr
    )
