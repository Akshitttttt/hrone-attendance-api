from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import main


def test_late_leaderboard_competition_ranks_ties_and_limit():
    original_db = main.db
    test_db = main.client["attendance_test_db"]
    main.db = test_db

    codes = ["EMP9401", "EMP9402", "EMP9403", "EMP9404", "EMP9405"]

    try:
        test_db.employees.delete_many({"emp_code": {"$in": codes}})
        test_db.attendance_logs.delete_many({"emp_code": {"$in": codes}})

        employees = [
            {
                "emp_code": "EMP9401",
                "name": "Rank One",
                "department": "QA",
                "joined_on": "2026-01-01",
            },
            {
                "emp_code": "EMP9402",
                "name": "Rank Two A",
                "department": "QA",
                "joined_on": "2026-01-01",
            },
            {
                "emp_code": "EMP9403",
                "name": "Rank Two B",
                "department": "QA",
                "joined_on": "2026-01-01",
            },
            {
                "emp_code": "EMP9404",
                "name": "Rank Four",
                "department": "Engineering",
                "joined_on": "2026-01-01",
            },
            {
                "emp_code": "EMP9405",
                "name": "No Late Minutes",
                "department": "QA",
                "joined_on": "2026-01-01",
            },
        ]
        test_db.employees.insert_many(employees)

        late_values = [
            ("EMP9401", 30),
            ("EMP9402", 20),
            ("EMP9403", 20),
            ("EMP9404", 10),
            ("EMP9405", 0),
        ]

        test_db.attendance_logs.insert_many([
            {
                "emp_code": code,
                "date": "2026-07-06",
                "status": "PRESENT",
                "late_minutes": minutes,
                "punch_in": datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
                "punch_out": None,
            }
            for code, minutes in late_values
        ])

        # Orphan attendance records must not appear in the leaderboard.
        test_db.attendance_logs.insert_one({
            "emp_code": "EMP9499",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 100,
            "punch_in": datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
            "punch_out": None,
        })

        with TestClient(main.app) as client:
            response = client.get(
                "/analytics/leaderboard/late",
                params={"month": "2026-07", "limit": 2},
            )
            assert response.status_code == 200
            items = response.json()["items"]

            assert [item["emp_code"] for item in items] == [
                "EMP9401", "EMP9402", "EMP9403"
            ]
            assert [item["rank"] for item in items] == [1, 2, 2]

            response = client.get(
                "/analytics/leaderboard/late",
                params={
                    "month": "2026-07",
                    "department": "Engineering",
                },
            )
            assert response.status_code == 200
            engineering = response.json()["items"]
            assert len(engineering) == 1
            assert engineering[0]["emp_code"] == "EMP9404"
            assert engineering[0]["rank"] == 1

    finally:
        test_db.employees.delete_many({"emp_code": {"$in": codes}})
        test_db.attendance_logs.delete_many(
            {"emp_code": {"$in": codes + ["EMP9499"]}}
        )
        main.db = original_db


def test_late_leaderboard_rejects_invalid_month_and_limit():
    with TestClient(main.app) as client:
        assert client.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-13"},
        ).status_code == 422

        assert client.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-07", "limit": 51},
        ).status_code == 422
