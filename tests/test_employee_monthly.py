from datetime import datetime, timezone

from fastapi.testclient import TestClient
from app import main


def test_monthly_summary_handles_weekends_half_days_and_join_date():
    original_db = main.db
    test_db = main.client["attendance_test_db"]
    main.db = test_db

    try:
        test_db.employees.delete_many({"emp_code": "EMP9201"})
        test_db.attendance_logs.delete_many({"emp_code": "EMP9201"})

        test_db.employees.insert_one({
            "emp_code": "EMP9201",
            "name": "Monthly Test",
            "email": "monthly@example.com",
            "department": "Testing",
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-07-15",
            "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
        })

        # July 15, 2026 is Wednesday. July 18 is Saturday.
        test_db.attendance_logs.insert_many([
            {
                "emp_code": "EMP9201",
                "date": "2026-07-15",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 15, 3, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
                "work_hours": 9.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            },
            {
                "emp_code": "EMP9201",
                "date": "2026-07-16",
                "status": "WFH",
                "punch_in": datetime(2026, 7, 16, 3, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 16, 6, tzinfo=timezone.utc),
                "work_hours": 3.0,
                "late_minutes": 12,
                "overtime_minutes": 0,
                "half_day": True,
            },
            {
                "emp_code": "EMP9201",
                "date": "2026-07-18",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 18, 3, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 18, 12, tzinfo=timezone.utc),
                "work_hours": 9.0,
                "late_minutes": 20,
                "overtime_minutes": 0,
                "half_day": False,
            },
            {
                "emp_code": "EMP9201",
                "date": "2026-07-17",
                "status": "LEAVE",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            },
        ])

        with TestClient(main.app) as client:
            response = client.get(
                "/analytics/employees/EMP9201/monthly",
                params={"month": "2026-07"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["working_days"] == 13
        assert data["present_days"] == 1.5
        assert data["leave_days"] == 1
        assert data["late_count"] == 2
        assert data["total_late_minutes"] == 32
        assert data["total_overtime_minutes"] == 0
        assert data["attendance_pct"] == 11.5385

    finally:
        test_db.employees.delete_many({"emp_code": "EMP9201"})
        test_db.attendance_logs.delete_many({"emp_code": "EMP9201"})
        main.db = original_db


def test_monthly_summary_rejects_invalid_month():
    with TestClient(main.app) as client:
        response = client.get(
            "/analytics/employees/EMP0001/monthly",
            params={"month": "2026-13"},
        )

    assert response.status_code == 422


def test_monthly_summary_unknown_employee_returns_404():
    with TestClient(main.app) as client:
        response = client.get(
            "/analytics/employees/EMP9999/monthly",
            params={"month": "2026-07"},
        )

    assert response.status_code == 404
