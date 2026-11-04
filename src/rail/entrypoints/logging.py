"""Configure console and JSONL diagnostics at the CLI entry point."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog


if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger


logger = logging.getLogger(name="workflow")


def add_log_context(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Add application identity while preserving event data.

    :param logger: the processor-supplied logger.
    :param method_name: the processor-supplied event method.
    :param event_dict: the event fields to normalize.

    :return: the normalized event dictionary.
    """
    del logger, method_name
    event_dict["source"] = event_dict.pop("logger")
    event_dict["application"] = "rail"
    return event_dict


def render_console_log(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> str:
    """Render human-readable diagnostics with one application prefix.

    :param logger: the processor-supplied logger.
    :param method_name: the event's level method.
    :param event_dict: the normalized event fields.

    :return: the prefixed console record with escaped embedded newlines.
    """
    rendered = structlog.dev.ConsoleRenderer(colors=False)(
        logger=logger, name=method_name, event_dict=event_dict
    )
    return "[RAIL] " + rendered.replace("\n", "\\n").replace("\r", "\\r")


def configure_logging(*, format_name: str) -> logging.Logger:
    """Configure one CLI logger without accumulating handlers on repeated calls.

    :param format_name: the validated console or json output selection.

    :return: the application logger writing diagnostics to stderr.
    """
    processors: list[Processor] = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        add_log_context,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if format_name == "json"
        else render_console_log
    )
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        fmt=structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    for previous in logger.handlers:
        previous.close()
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(level=logging.INFO)
    structlog.configure(
        processors=[
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    return logger
