"""Define shared runtime limits without configuration I/O dependencies."""

from typing import Final


class RuntimeConfig:
    """Define shared runtime limits.

    :attr CAPTURE_LIMIT_BYTES: the maximum captured bytes per subprocess stream.
    :attr QUERY_TIMEOUT_SECONDS: the deadline for external queries.
    :attr CHECK_TIMEOUT_SECONDS: the deadline per configured check attempt.
    :attr CHILD_STOP_TIMEOUT_SECONDS: the wait after killing the owned process.
    :attr DESCENDANT_STOP_TIMEOUT_SECONDS: the wait for killed descendants to exit.
    :attr LOCK_TIMEOUT_SECONDS: the wait for another repository operation to finish.
    :attr PR_LOOKUP_LIMIT: the result count needed to detect ambiguous PR lookup.
    :attr PR_HEAD_READ_ATTEMPTS: the maximum reads while awaiting a published head.
    :attr PR_HEAD_RETRY_DELAY_SECONDS: the delay between PR head verification reads.
    :attr MAX_CHECK_RETRIES: the largest supported additional check attempt count.
    """

    CAPTURE_LIMIT_BYTES: Final = 1_048_576
    QUERY_TIMEOUT_SECONDS: Final = 300
    CHECK_TIMEOUT_SECONDS: Final = 3_600
    CHILD_STOP_TIMEOUT_SECONDS: Final = 10
    DESCENDANT_STOP_TIMEOUT_SECONDS: Final = 5
    LOCK_TIMEOUT_SECONDS: Final = 0
    PR_LOOKUP_LIMIT: Final = 2
    PR_HEAD_READ_ATTEMPTS: Final = 5
    PR_HEAD_RETRY_DELAY_SECONDS: Final = 1
    MAX_CHECK_RETRIES: Final = 2
