"""The publisher's audit log: hard failures are kept durably, routine
operation is not."""

from logging.handlers import RotatingFileHandler

import pytest

from collegium.publisher import _is_hard_failure, configure_audit_log, log


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("halted: the last 3 verifications failed; the owner must look", True),
        ("post 1: verification failed", True),
        ("post 1: failed: no id", True),
        ("vote 1: failed: ConnectError: ...", True),
        ("error: OperationalError: could not connect", True),
        ("post 1: rate limited, will retry", False),
        ("post 1: published at https://www.moltbook.com/posts/1", False),
        ("vote 1: upvoted https://www.moltbook.com/posts/1", False),
        ("nothing to publish now", False),
    ],
)
def test_hard_failures_are_told_from_routine_operation(outcome, expected):
    assert _is_hard_failure(outcome) == expected


def test_the_audit_log_keeps_warnings_and_not_routine_logging(tmp_path):
    configure_audit_log(str(tmp_path))
    try:
        log.info("nothing to publish now")
        log.warning("halted: the last 3 verifications failed")
    finally:
        # Leave the module logger as we found it for every other test.
        for handler in [h for h in log.handlers if isinstance(h, RotatingFileHandler)]:
            log.removeHandler(handler)
            handler.close()

    content = (tmp_path / "publisher.log").read_text()
    assert "halted" in content
    assert "nothing to publish now" not in content


def test_disabled_when_no_directory_is_configured(tmp_path):
    before = list(log.handlers)
    configure_audit_log(None)
    assert log.handlers == before
    assert not (tmp_path / "publisher.log").exists()


def test_an_unwritable_directory_does_not_crash_the_publisher(tmp_path, caplog):
    """Seen live: a bind-mounted directory Docker creates as root, which
    this process's own non-root user cannot write to. Publishing must
    keep working regardless; only the audit trail itself is lost."""
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir(mode=0o000)
    try:
        before = list(log.handlers)
        configure_audit_log(str(unwritable))
        assert log.handlers == before  # no handler added
        assert "could not set up the audit log" in caplog.text
    finally:
        unwritable.chmod(0o755)  # so pytest can clean up tmp_path
