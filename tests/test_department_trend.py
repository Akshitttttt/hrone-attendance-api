from fastapi.testclient import TestClient

from app import main


def test_department_trend_densifies_dates_and_calculates_rates():
    original_db = main.db
    test_db = main.client["attendance_test_db"]
    main.db = test_db

    codes = ["EMP9501", "EMP9502"]

    try:
        test_db.employees.delete_many({"emp_code": {"$in": codes}})
        test_db.attendance_logs.delete_many({"emp_code": {"$in": codes}})

        test_db.employees.insert_many([
            {
                "emp_code": "EMP9501",
                "name": "Trend One",
                "department": "TrendQA",
                "joined_on": "2026-07-06",
            },
            {
                "emp_code": "EMP9502",
                "name": "Trend Two",
                "department": "TrendQA",
                "joined_on": "2026-07-08",
            },
        ])

        test_db.attendance_logs.insert_many([
            {
                "emp_code": "EMP9501",
                "date": "2026-07-06",
                "status": "PRESENT",
                "half_day": False,
                "late_minutes": 15,
            },
            {
                "emp_code": "EMP9501",
                "date": "2026-07-08",
                "status": "PRESENT",
                "half_day": True,
                "late_minutes": 0,
            },
            {
                "emp_code": "EMP9502",
                "date": "2026-07-08",
                "status": "WFH",
                "half_day": False,
                "late_minutes": 5,
            },
            {
                "emp_code": "EMP9501",
                "date": "2026-07-11",
                "status": "PRESENT",
                "half_day": False,
                "late_minutes": 0,
            },
        ])

        with TestClient(main.app) as client:
            response = client.get(
                "/analytics/departments/TrendQA/trend",
                params={
                    "date_from": "2026-07-06",
                    "date_to": "2026-07-12",
                },
            )

        assert response.status_code == 200, response.text
        items = response.json()["items"]

        # All seven dates must be present, including dates without attendance.
        assert len(items) == 7
        assert [item["date"] for item in items] == [
            "2026-07-06",
            "2026-07-07",
            "2026-07-08",
            "2026-07-09",
            "2026-07-10",
            "2026-07-11",
            "2026-07-12",
        ]

        monday = items[0]
        assert monday["headcount"] == 1
        assert monday["present_count"] == 1.0
        assert monday["attendance_rate"] == 100.0
        assert monday["late_count"] == 1

        tuesday = items[1]
        assert tuesday["headcount"] == 1
        assert tuesday["present_count"] == 0
        assert tuesday["attendance_rate"] == 0.0

        wednesday = items[2]
        assert wednesday["headcount"] == 2
        assert wednesday["present_count"] == 1.5
        assert wednesday["attendance_rate"] == 75.0
        assert wednesday["late_count"] == 1

        saturday = items[5]
        assert saturday["is_working_day"] is False
        assert saturday["present_count"] == 1.0
        assert saturday["attendance_rate"] is None

        sunday = items[6]
        assert sunday["is_working_day"] is False
        assert sunday["present_count"] == 0
        assert sunday["attendance_rate"] is None

        # Null weekend rates are excluded from the moving average.
        assert items[4]["moving_avg_7d"] == 35.0

    finally:
        test_db.employees.delete_many({"emp_code": {"$in": codes}})
        test_db.attendance_logs.delete_many({"emp_code": {"$in": codes}})
        main.db = original_db


def test_department_trend_rejects_invalid_ranges_and_unknown_department():
    with TestClient(main.app) as client:
        inverted = client.get(
            "/analytics/departments/TrendQA/trend",
            params={
                "date_from": "2026-07-10",
                "date_to": "2026-07-09",
            },
        )
        assert inverted.status_code == 422

        too_long = client.get(
            "/analytics/departments/TrendQA/trend",
            params={
                "date_from": "2026-01-01",
                "date_to": "2026-04-03",
            },
        )
        assert too_long.status_code == 422

        unknown = client.get(
            "/analytics/departments/DepartmentThatDoesNotExist/trend",
            params={
                "date_from": "2026-07-06",
                "date_to": "2026-07-07",
            },
        )
        assert unknown.status_code == 404
