"""Check logging formats, field preservation, and exception chains."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import Mock

import structlog

from rail.entrypoints.logging import configure_logging


if TYPE_CHECKING:
    import logging

    import pytest


def test_json_records(*, capsys: pytest.CaptureFixture[str]) -> None:
    """Context: standard and structlog events share JSONL output configuration.

    Invariants:
        - Each event is one JSON object with UTC identity and typed fields.
        - Newlines are encoded and text fields retain their contents.
        - Reconfiguration does not duplicate events.
    """
    configure_logging(format_name="json")
    logger = configure_logging(format_name="json")
    logger.info(
        msg="line one\nline two",
        extra={"count": 3, "ready": True, "detail": "command detail"},
    )
    structlog.get_logger("workflow").info(event="Structured event", count=2)
    records = [json.loads(s=line) for line in capsys.readouterr().err.splitlines()]
    assert len(records) == 2
    assert records[0]["count"] == 3
    assert records[0]["ready"] is True
    assert records[0]["event"] == "line one\nline two"
    assert records[0]["detail"] == "command detail"
    for record in records:
        assert record["timestamp"].endswith("Z")
        assert record["source"] == "workflow"
        assert record["application"] == "rail"
        assert record["level"] == "info"
    assert records[1]["count"] == 2


def test_exception_chain(*, capsys: pytest.CaptureFixture[str]) -> None:
    """Context: an unexpected exception is logged with its chained cause.

    Invariants:
        - The chain and its messages remain within one JSONL record.
    """
    logger = configure_logging(format_name="json")
    try:
        try:
            raise ValueError("inner failure")
        except ValueError as error:
            raise RuntimeError("outer failure") from error
    except RuntimeError:
        logger.exception(msg="Unexpected failure")
    output = capsys.readouterr().err
    record = json.loads(s=output)
    assert len(output.splitlines()) == 1
    assert "ValueError" in record["exception"]
    assert "RuntimeError" in record["exception"]
    assert "inner failure" in record["exception"]


def test_console_prefix(*, capsys: pytest.CaptureFixture[str]) -> None:
    """Context: an ordinary console diagnostic includes an embedded newline.

    Invariants:
        - Exactly one prefix appears at the beginning of a single physical line.
    """
    logger: logging.Logger = configure_logging(format_name="console")
    logger.warning(msg="first\nsecond")
    output = capsys.readouterr().err
    assert output.startswith("[RAIL] ")
    assert output.count("[RAIL]") == 1
    assert len(output.splitlines()) == 1
    assert "first\\nsecond" in output


def test_delivery_failure_does_not_propagate(
    *, capsys: pytest.CaptureFixture[str]
) -> None:
    """Context: the standard logging handler encounters a failed output stream.

    Invariants:
        - Diagnostic delivery failure does not replace the caller's result.
        - The standard logging fallback reports the delivery problem.
    """
    logger = configure_logging(format_name="console")
    logger.handlers[0].stream = Mock(write=Mock(side_effect=OSError("broken stream")))
    assert logger.error(msg="Original operation failed") is None
    assert "Logging error" in capsys.readouterr().err
