from datetime import datetime, timezone

from fastapi.testclient import TestClient
from app import main


def test_department_summary_counts_zero_log_employees_and_filters_department():
    original_db = main.db
    test_db = main.client["attendance_test_db"]
    main.db = test_db

    try:
        test_db.employees.delete_many({"emp_code": {"$in": ["EMP9301", "EMP9302", "EMP9303"]}})
        test_db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9301", "EMP9302", "EMP9303"]}})

        test_db.employees.insert_many([
            {
                "emp_code": "EMP9301", "name": "Worker One",
                "email": "one@example.com", "department": "QA",
                "shift_start": "09:00", "shift_end": "18:00",
                "joined_on": "2026-07-01",
                "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
            },
            {
                "emp_code": "EMP9302", "name": "Worker Two",
                "email": "two@example.com", "department": "QA",
                "shift_start": "09:00", "shift_end": "18:00",
                "joined_on": "2026-07-01",
                "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
            },
            {
                "emp_code": "EMP9303", "name": "Late Joiner",
                "email": "three@example.com", "department": "QA",
                "shift_start": "09:00", "shift_end": "18:00",
                "joined_on": "2026-08-01",
                "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            },
        ])

        test_db.attendance_logs.insert_one({
            "emp_code": "EMP9301",
            "date": "2026-07-06",  # Monday
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 3, 30, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 12, 30, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": False,
        })

        with TestClient(main.app) as client:
            response = client.get(
                "/analytics/departments/summary",
                params={"month": "2026-07", "department": "QA"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) == 1

        qa = data["items"][0]
        assert qa["department"] == "QA"
        assert qa["headcount"] == 2  # zero-log employee counts; August joiner excluded
        assert qa["present_days"] == 1.0
        assert qa["late_count"] == 1
        assert qa["total_late_minutes"] == 15
        assert qa["avg_work_hours"] == 9.0
        assert qa["leave_count"] == 0
        assert qa["on_duty_count"] == 0

    finally:
        test_db.employees.delete_many({"emp_code": {"$in": ["EMP9301", "EMP9302", "EMP9303"]}})
        test_db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9301", "EMP9302", "EMP9303"]}})
        main.db = original_db


def test_department_summary_rejects_invalid_month():
    with TestClient(main.app) as client:
        response = client.get(
            "/analytics/departments/summary",
            params={"month": "2026-13"},
        )

    assert response.status_code == 422
