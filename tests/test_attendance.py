from datetime import datetime
from zoneinfo import ZoneInfo

from app.main import compute_overtime, compute_work_hours

IST = ZoneInfo("Asia/Kolkata")


def test_work_hours_round_to_two_decimals():
    start = datetime(2026, 1, 1, 9, 0, tzinfo=IST)
    end = datetime(2026, 1, 1, 13, 30, tzinfo=IST)

    assert compute_work_hours(start, end) == 4.50


def test_overtime_below_30_minutes_is_zero():
    punch_out = datetime(2026, 1, 1, 18, 59, tzinfo=IST)

    assert compute_overtime(
        punch_out, "09:30", "18:30", "2026-01-01"
    ) == 0


def test_overtime_at_30_minutes_is_counted():
    punch_out = datetime(2026, 1, 1, 19, 0, tzinfo=IST)

    assert compute_overtime(
        punch_out, "09:30", "18:30", "2026-01-01"
    ) == 30
