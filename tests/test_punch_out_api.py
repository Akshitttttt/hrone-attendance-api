from datetime import datetime, timezone

from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as main


def test_punch_out_success():
    client = MongoClient(
        "mongodb://localhost:27017",
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    test_db = client["attendance_test_db"]

    original_db = main.db
    main.db = test_db

    try:
        # Clear only the dedicated test database.
        test_db.employees.delete_many({})
        test_db.attendance_logs.delete_many({})

        test_db.employees.insert_one({
            "emp_code": "EMP9001",
            "name": "Test Employee",
            "email": "test9001@example.com",
            "department": "Testing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        punch_in = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
        test_db.attendance_logs.insert_one({
            "emp_code": "EMP9001",
            "date": "2026-01-01",
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })

        punch_out_ms = int(
            datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc).timestamp()
        ) * 1000

        with TestClient(main.app) as api:
            response = api.post(
                "/attendance/punch-out",
                json={"emp_code": "EMP9001", "punched_at": punch_out_ms},
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["emp_code"] == "EMP9001"
        assert data["work_hours"] == 9.0
        assert data["punch_out"] == punch_out_ms

        saved = test_db.attendance_logs.find_one({"emp_code": "EMP9001"})
        assert saved["punch_out"] is not None
    finally:
        main.db = original_db
        client.close()


def test_punch_out_unknown_employee_returns_404():
    client = MongoClient(
        "mongodb://localhost:27017",
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    test_db = client["attendance_test_db"]
    original_db = main.db
    main.db = test_db

    try:
        test_db.employees.delete_many({})
        test_db.attendance_logs.delete_many({})

        with TestClient(main.app) as api:
            response = api.post(
                "/attendance/punch-out",
                json={"emp_code": "EMP9999"},
            )

        assert response.status_code == 404
        assert response.json()["detail"] == "employee not found"
    finally:
        main.db = original_db
        client.close()


def test_punch_out_before_punch_in_returns_422():
    client = MongoClient(
        "mongodb://localhost:27017",
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    test_db = client["attendance_test_db"]
    original_db = main.db
    main.db = test_db

    try:
        test_db.employees.delete_many({})
        test_db.attendance_logs.delete_many({})

        test_db.employees.insert_one({
            "emp_code": "EMP9002",
            "name": "Test Employee",
            "email": "test9002@example.com",
            "department": "Testing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        punch_in = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        test_db.attendance_logs.insert_one({
            "emp_code": "EMP9002",
            "date": "2026-01-01",
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })

        earlier_ms = int(
            datetime(2026, 1, 1, 9, 59, tzinfo=timezone.utc).timestamp()
        ) * 1000

        with TestClient(main.app) as api:
            response = api.post(
                "/attendance/punch-out",
                json={"emp_code": "EMP9002", "punched_at": earlier_ms},
            )

        assert response.status_code == 422
    finally:
        main.db = original_db
        client.close()
