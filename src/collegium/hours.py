"""Working hours: when the organization works on its own.

The owner prefers to watch what happens, so the worker only starts jobs,
and the scheduler only plans, within working hours. Work queued outside
them waits for the next opening; a job still running at closing time
finishes.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class WorkingHours:
    start: time
    end: time  # time.max for "until midnight"
    days: frozenset[int]  # 0 = Monday
    tz: ZoneInfo

    @classmethod
    def parse(cls, hours: str, days: str, tz: str) -> "WorkingHours":
        """hours "08:00-16:00" or "always"; days "mon-fri" or "mon,wed,fri"."""
        zone = ZoneInfo(tz)
        if hours.strip().lower() == "always":
            return cls(time.min, time.max, frozenset(range(7)), zone)
        start, end = (part.strip() for part in hours.split("-"))
        return cls(
            time.fromisoformat(start),
            time.max if end == "24:00" else time.fromisoformat(end),
            _parse_days(days),
            zone,
        )

    def local(self, now: datetime | None = None) -> datetime:
        return (now or datetime.now(self.tz)).astimezone(self.tz)

    def is_open(self, now: datetime | None = None) -> bool:
        t = self.local(now)
        return t.weekday() in self.days and self.start <= t.time() < self.end

    def workday(self, now: datetime | None = None) -> date:
        return self.local(now).date()

    def start_of_workday(self, now: datetime | None = None) -> datetime:
        """Midnight at the start of the local day, as an aware datetime."""
        return datetime.combine(self.workday(now), time.min, self.tz)

    def next_opening(self, now: datetime | None = None) -> datetime:
        t = self.local(now)
        for offset in range(8):
            day = t.date() + timedelta(days=offset)
            opening = datetime.combine(day, self.start, self.tz)
            if day.weekday() in self.days and opening > t:
                return opening
        return t + timedelta(days=1)

    def describe(self) -> str:
        if self.start == time.min and self.end == time.max and len(self.days) == 7:
            return "always"
        days = ",".join(DAYS[d] for d in sorted(self.days))
        end = "24:00" if self.end == time.max else f"{self.end:%H:%M}"
        return f"{days} {self.start:%H:%M}-{end} {self.tz.key}"


def _parse_days(spec: str) -> frozenset[int]:
    days: set[int] = set()
    for part in spec.lower().split(","):
        part = part.strip()
        if "-" in part:
            first, last = (DAYS.index(p.strip()[:3]) for p in part.split("-"))
            days |= set(range(first, last + 1))
        elif part:
            days.add(DAYS.index(part[:3]))
    return frozenset(days)
