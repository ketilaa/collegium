"""Charts drawn on the server as SVG geometry, so the page needs no chart
library and runs under a strict content security policy."""

from dataclasses import dataclass
from datetime import datetime

from collegium.roles.historian import ACCEPT_AT

WIDTH, HEIGHT = 640, 200
LEFT, RIGHT, TOP, BOTTOM = 40, 16, 12, 28


@dataclass
class Point:
    x: float
    y: float
    confidence: float
    at: datetime
    by: str


@dataclass
class Chart:
    width: int
    height: int
    points: list[Point]
    line: str  # SVG polyline points
    grid: list[tuple[float, str]]  # y position, label
    threshold: float  # y position of the acceptance line
    threshold_value: float
    x_labels: list[tuple[float, datetime]]
    plot_left: float
    plot_right: float


def confidence_chart(history: list[dict]) -> Chart | None:
    """Confidence over time, on a fixed 0-1 scale with the Historian's
    acceptance threshold marked. None when there is nothing to draw."""
    if not history:
        return None
    left, right = LEFT, WIDTH - RIGHT
    top, bottom = TOP, HEIGHT - BOTTOM
    first, last = history[0]["assessed_at"], history[-1]["assessed_at"]
    span = (last - first).total_seconds()

    def x(i: int, at: datetime) -> float:
        if span <= 0:
            # All at once (or a single assessment): spread them evenly.
            return (
                (left + right) / 2
                if len(history) == 1
                else left + i * (right - left) / (len(history) - 1)
            )
        return left + (at - first).total_seconds() / span * (right - left)

    def y(value: float) -> float:
        return bottom - value * (bottom - top)

    points = [
        Point(
            round(x(i, c["assessed_at"]), 1),
            round(y(c["confidence"]), 1),
            c["confidence"],
            c["assessed_at"],
            c["assessed_by"],
        )
        for i, c in enumerate(history)
    ]
    return Chart(
        width=WIDTH,
        height=HEIGHT,
        points=points,
        line=" ".join(f"{p.x},{p.y}" for p in points),
        grid=[(round(y(v), 1), f"{v:.1f}") for v in (0.0, 0.5, 1.0)],
        threshold=round(y(ACCEPT_AT), 1),
        threshold_value=ACCEPT_AT,
        x_labels=[(points[0].x, first)] + ([(points[-1].x, last)] if span > 0 else []),
        plot_left=left,
        plot_right=right,
    )
