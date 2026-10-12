from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as main


@pytest.fixture
def attendance_api():
    client = MongoClient(
        "mongodb://localhost:27017",
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    test_db = client["attendance_test_db"]
    original_db = main.db
    main.db = test_db

    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})

    test_db.employees.insert_one({
        "emp_code": "EMP9101",
        "name": "Correction Test",
        "email": "correction@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    test_db.attendance_logs.insert_one({
        "emp_code": "EMP9101",
        "date": "2026-01-01",
        "status": "PRESENT",
        "punch_in": datetime(2026, 1, 1, 4, 35, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
        "work_hours": 8.42,
        "late_minutes": 35,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    try:
        with TestClient(main.app) as api:
            yield api, test_db
    finally:
        main.db = original_db
        client.close()


def test_regularize_recomputes_fields_and_appends_history(attendance_api):
    api, test_db = attendance_api
    corrected_punch_in = int(
        datetime(2026, 1, 1, 3, 58, tzinfo=timezone.utc).timestamp()
    ) * 1000

    response = api.patch(
        "/attendance/EMP9101/2026-01-01",
        json={
            "punch_in": corrected_punch_in,
            "reason": "Biometric clock correction",
            "regularized_by": "hr.admin",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["punch_in"] == corrected_punch_in
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03
    assert len(data["history"]) == 1

    changes = data["history"][0]["changes"]
    assert changes["punch_in"]["from"] is not None
    assert changes["punch_in"]["to"] == corrected_punch_in
    assert changes["late_minutes"] == {"from": 35, "to": 0}
    assert changes["work_hours"] == {"from": 8.42, "to": 9.03}

    saved = test_db.attendance_logs.find_one({"emp_code": "EMP9101"})
    assert len(saved["history"]) == 1
    assert saved["late_minutes"] == 0


def test_regularize_no_change_returns_422(attendance_api):
    api, test_db = attendance_api

    response = api.patch(
        "/attendance/EMP9101/2026-01-01",
        json={
            "status": "PRESENT",
            "reason": "No actual change",
            "regularized_by": "hr.admin",
        },
    )

    assert response.status_code == 422, response.text
    saved = test_db.attendance_logs.find_one({"emp_code": "EMP9101"})
    assert saved["history"] == []


def test_regularize_rejects_punch_in_on_wrong_attendance_date(attendance_api):
    api, test_db = attendance_api
    next_day_punch_in = int(
        datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc).timestamp()
    ) * 1000

    response = api.patch(
        "/attendance/EMP9101/2026-01-01",
        json={
            "punch_in": next_day_punch_in,
            "reason": "Correct the recorded punch",
            "regularized_by": "hr.admin",
        },
    )

    assert response.status_code == 422, response.text
    saved = test_db.attendance_logs.find_one({"emp_code": "EMP9101"})
    assert saved["history"] == []


def test_regularize_unknown_record_returns_404(attendance_api):
    api, _ = attendance_api

    response = api.patch(
        "/attendance/EMP9101/2026-01-02",
        json={
            "status": "LEAVE",
            "reason": "Approved leave entry",
            "regularized_by": "hr.admin",
        },
    )

    assert response.status_code == 404
