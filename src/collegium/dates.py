"""Lenient date parsing for dates from search APIs and models."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_date(value: str | None) -> datetime | None:
    """ISO 8601 or RFC 2822, as a timezone-aware datetime; None if the value
    is missing or not a date. A wrong date is worse than none."""
    if not value:
        return None
    for parse in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            moment = parse(value.strip())
        except (ValueError, TypeError, IndexError):
            continue
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return None
