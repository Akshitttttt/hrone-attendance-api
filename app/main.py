"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
import re
from decimal import Decimal, ROUND_HALF_UP

from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once at startup. create_index is idempotent.
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index(
        [("department", 1), ("joined_on", 1), ("emp_code", 1)],
        name="department_joined_on_emp_code",
    )
    db.employees.create_index(
        [("joined_on", 1), ("department", 1), ("emp_code", 1)],
        name="joined_on_department_emp_code",
    )
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime from MongoDB -> epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str):
    """R1: the IST date of the punch-in, or the previous day for an overnight shift's early-morning punch."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    overnight = parse_hhmm(shift_end) <= parse_hhmm(shift_start)
    if overnight and ist.time() < parse_hhmm(shift_end):
        day = day - timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    start = datetime.combine(att_date, parse_hhmm(shift_start), tzinfo=IST)
    seconds = (punch_in - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_date(value: str) -> str:
    """Accept only a real calendar date written YYYY-MM-DD."""
    if not DATE_RE.match(value):
        raise ValueError("must be YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def date_param(value: Optional[str], name: str) -> Optional[str]:
    """Same check for query parameters; a bad value is a 422, never a 500."""
    if value is None:
        return None
    try:
        return check_date(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")



def month_bounds(month: str) -> tuple[str, str]:
    """Validate YYYY-MM and return inclusive month boundaries."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be in YYYY-MM format")

    year, month_number = map(int, month.split("-"))
    start = datetime(year, month_number, 1).date()
    if month_number == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, month_number + 1, 1).date()
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat()


def round_half_up(value, places: int) -> float:
    """Round using decimal half-up semantics."""
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def to_api(doc: dict) -> dict:
    """Shape a stored attendance document for the API: no _id, instants as epoch ms, gaps filled."""
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                change = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
            changes[field] = change
        history.append(
            {"at": to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: Calculate work hours rounded to two decimal places, half-up."""
    elapsed_seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(elapsed_seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """R3: Count whole overtime minutes only when at least 30 minutes are worked beyond shift end."""
    start_time = parse_hhmm(shift_start)
    end_time = parse_hhmm(shift_end)
    attendance_day = datetime.strptime(date_str, "%Y-%m-%d").date()

    overnight = end_time <= start_time
    shift_end_day = attendance_day + timedelta(days=1) if overnight else attendance_day
    shift_end_dt = datetime.combine(shift_end_day, end_time, tzinfo=IST)

    elapsed_seconds = (punch_out - shift_end_dt).total_seconds()
    if elapsed_seconds < 30 * 60:
        return 0
    return int(elapsed_seconds // 60)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_date(cls, value: str) -> str:
        return check_date(value)

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"



class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None



class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = (
        from_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    # Find the most recent punch-in for this employee.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")

    # Reject a punch-out timestamp that is not after the selected punch-in.
    if punched_at <= record["punch_in"]:
        raise HTTPException(422, "punch-out must be after punch-in")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_at = record["punch_in"]
    elapsed_seconds = (punched_at - punch_in_at).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")

    work_hours = compute_work_hours(punch_in_at, punched_at)
    overtime = compute_overtime(
        punched_at,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )
    half_day = work_hours < 4.50

    # Conditional update ensures only one concurrent request closes this record.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
            "punch_in": {"$lte": punched_at},
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record was already updated")

    record.update(
        {
            "punch_out": punched_at,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }
    )
    return to_api(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = date_param(date_from, "date_from")
    date_to = date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    date = date_param(date, "date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if original is None:
        raise HTTPException(404, "attendance record not found")

    # Distinguish omitted fields from explicitly supplied nulls.
    supplied = body.model_fields_set
    if "punch_in" in supplied and body.punch_in is None:
        raise HTTPException(422, "punch_in cannot be null")
    if "punch_out" in supplied and body.punch_out is None:
        raise HTTPException(422, "punch_out cannot be null")

    old = dict(original)
    new_status = body.status if "status" in supplied else old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if "punch_in" in supplied else old.get("punch_in")
        new_out = from_ms(body.punch_out) if "punch_out" in supplied else old.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")

        expected_date = attendance_date(
            new_in, employee["shift_start"], employee["shift_end"]
        ).isoformat()
        if expected_date != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")

        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_status in ("ABSENT", "LEAVE"):
        derived = {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        derived = {
            "work_hours": compute_work_hours(new_in, new_out) if new_out else None,
            "late_minutes": compute_late_minutes(
                new_in, employee["shift_start"], datetime.strptime(date, "%Y-%m-%d").date()
            ),
            "overtime_minutes": compute_overtime(
                new_out, employee["shift_start"], employee["shift_end"], date
            ) if new_out else 0,
            "half_day": compute_work_hours(new_in, new_out) < 4.50 if new_out else False,
        }

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }

    def comparable(value):
        return to_ms(value) if isinstance(value, datetime) else value

    changes = {}
    for field, value in new_values.items():
        before = old.get(field)
        if comparable(before) != comparable(value):
            changes[field] = {"from": before, "to": value}

    if not changes:
        raise HTTPException(422, "correction does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-set protects against concurrent edits and lost history.
    expected = {
        "_id": original["_id"],
        "status": old["status"],
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        "work_hours": old.get("work_hours"),
        "late_minutes": old.get("late_minutes", 0),
        "overtime_minutes": old.get("overtime_minutes", 0),
        "half_day": old.get("half_day", False),
        "history": old.get("history", []),
    }
    update = db.attendance_logs.update_one(
        expected,
        {"$set": new_values, "$push": {"history": history_entry}},
    )
    if update.modified_count != 1:
        raise HTTPException(409, "attendance record changed concurrently; retry correction")

    original.update(new_values)
    original.setdefault("history", []).append(history_entry)
    return to_api(original)



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    month_start, month_end = month_bounds(month)

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    # Count weekdays from the later of month start and employee joining date.
    effective_start = max(month_start, employee["joined_on"])
    start_day = datetime.strptime(effective_start, "%Y-%m-%d").date()
    end_day = datetime.strptime(month_end, "%Y-%m-%d").date()
    working_days = 0

    if start_day <= end_day:
        day_count = (end_day - start_day).days + 1
        full_weeks, remaining_days = divmod(day_count, 7)
        working_days = full_weeks * 5
        for offset in range(remaining_days):
            day = start_day + timedelta(days=full_weeks * 7 + offset)
            if day.weekday() < 5:
                working_days += 1

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end},
        }},
        {"$set": {
            "_date": {"$dateFromString": {"dateString": "$date"}},
            "_half_day": {"$ifNull": ["$half_day", False]},
            "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "_overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gte": ["$_date", {"$dateFromString": {"dateString": effective_start}}]},
                    {"$lte": ["$_date", {"$dateFromString": {"dateString": month_end}}]},
                    {"$gte": [{"$dayOfWeek": "$_date"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$_date"}, 6]},
                ]},
                {"$cond": ["$_half_day", 0.5, 1.0]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late_minutes"},
            "total_overtime_minutes": {"$sum": "$_overtime_minutes"},
        }},
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    summary = result[0] if result else {}
    present_days = round_half_up(summary.get("present_days", 0), 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": (
            round_half_up(present_days / working_days * 100, 4)
            if working_days else None
        ),
    }




@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month_start, month_end = month_bounds(month)

    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", month_start]},
                    {"$lte": ["$date", month_end]},
                ]}}},
                {"$set": {
                    "_date": {"$dateFromString": {"dateString": "$date"}},
                    "_late": {"$ifNull": ["$late_minutes", 0]},
                    "_half": {"$ifNull": ["$half_day", False]},
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$gte": [{"$dayOfWeek": "$_date"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$_date"}, 6]},
                        ]},
                        {"$cond": ["$_half", 0.5, 1.0]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$_late"},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "hours_sum": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        "$work_hours", 0,
                    ]}},
                    "hours_count": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        1, 0,
                    ]}},
                }},
            ],
            "as": "_attendance",
        }},
        {"$set": {"_stats": {"$ifNull": [
            {"$arrayElemAt": ["$_attendance", 0]}, {}
        ]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$_stats.present_days", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$_stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$_stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$_stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$_stats.on_duty_count", 0]}},
            "hours_sum": {"$sum": {"$ifNull": ["$_stats.hours_sum", 0]}},
            "hours_count": {"$sum": {"$ifNull": ["$_stats.hours_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_count", 0]},
                {"$divide": ["$hours_sum", "$hours_count"]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]

    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)

    pipeline = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                        1,
                        0,
                    ]
                }
            },
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "_employee",
        }},
        {"$unwind": "$_employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$_employee.name",
            "department": "$_employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str,
    date_to: str,
):
    start_date = check_date(date_from)
    end_date = check_date(date_to)

    if start_date > end_date:
        raise HTTPException(422, "date_from must be on or before date_to")

    start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_day = datetime.strptime(end_date, "%Y-%m-%d").date()

    if (end_day - start_day).days > 91:
        raise HTTPException(422, "Date range must not exceed 92 days")

    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "Department not found")

    start_datetime = datetime.combine(start_day, datetime.min.time())
    end_datetime = datetime.combine(end_day, datetime.min.time())

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "department": department,
                            "joined_on": {"$lte": end_date},
                        }
                    }
                ],
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
        {
            "$set": {
                "_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                },
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_half": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": "$_date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$_half", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_datetime, end_datetime + timedelta(days=1)],
                },
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": {"$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": "$_id",
                }}},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                            "department": department,
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    for item in items:
        item["present_count"] = round_half_up(item["present_count"], 2)
        item["attendance_rate"] = (
            round_half_up(item["attendance_rate"] * 100, 4)
            if item["attendance_rate"] is not None
            else None
        )
        item["moving_avg_7d"] = (
            round_half_up(item["moving_avg_7d"] * 100, 4)
            if item["moving_avg_7d"] is not None
            else None
        )

    return {
        "department": department,
        "date_from": start_date,
        "date_to": end_date,
        "items": items,
    }
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50),
):
    from bson import json_util
    import json

    def require(value, name):
        if value is None:
            raise HTTPException(422, f"{name} is required")
        return value

    if endpoint == "attendance_list":
        start_date = date_param(date_from, "date_from")
        end_date = date_param(date_to, "date_to")
        if start_date and end_date and start_date > end_date:
            raise HTTPException(422, "date_from must not be after date_to")

        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if start_date or end_date:
            query["date"] = {}
            if start_date:
                query["date"]["$gte"] = start_date
            if end_date:
                query["date"]["$lte"] = end_date
        if status is not None:
            query["status"] = status

        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        month_start, month_end = month_bounds(require(month, "month"))
        code = require(emp_code, "emp_code")
        if db.employees.find_one({"emp_code": code}) is None:
            raise HTTPException(404, "employee not found")

        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": [
                {"$match": {
                    "emp_code": code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }},
                {"$set": {
                    "_date": {"$dateFromString": {"dateString": "$date"}},
                    "_half_day": {"$ifNull": ["$half_day", False]},
                    "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "_overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$gte": ["$_date", {"$dateFromString": {"dateString": max(month_start, db.employees.find_one({"emp_code": code})["joined_on"])}}]},
                            {"$lte": ["$_date", {"$dateFromString": {"dateString": month_end}}]},
                            {"$gte": [{"$dayOfWeek": "$_date"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$_date"}, 6]},
                        ]},
                        {"$cond": ["$_half_day", 0.5, 1.0]},
                        0,
                    ]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$_late_minutes"},
                    "total_overtime_minutes": {"$sum": "$_overtime_minutes"},
                }},
            ],
            "cursor": {},
        }

    elif endpoint == "department_summary":
        month_start, month_end = month_bounds(require(month, "month"))
        employee_filter = {"joined_on": {"$lte": month_end}}
        if department is not None:
            employee_filter["department"] = department

        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": [
                {"$match": employee_filter},
                {"$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$emp_code", "$$code"]},
                            {"$gte": ["$date", month_start]},
                            {"$lte": ["$date", month_end]},
                        ]}}},
                        {"$set": {
                            "_date": {"$dateFromString": {"dateString": "$date"}},
                            "_late": {"$ifNull": ["$late_minutes", 0]},
                            "_half": {"$ifNull": ["$half_day", False]},
                        }},
                        {"$group": {
                            "_id": None,
                            "present_days": {"$sum": {"$cond": [
                                {"$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": [{"$dayOfWeek": "$_date"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$_date"}, 6]},
                                ]},
                                {"$cond": ["$_half", 0.5, 1.0]},
                                0,
                            ]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                            "total_late_minutes": {"$sum": "$_late"},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                            "hours_sum": {"$sum": {"$cond": [
                                {"$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]},
                                "$work_hours", 0,
                            ]}},
                            "hours_count": {"$sum": {"$cond": [
                                {"$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]},
                                1, 0,
                            ]}},
                        }},
                    ],
                    "as": "_attendance",
                }},
                {"$set": {"_stats": {"$ifNull": [{"$arrayElemAt": ["$_attendance", 0]}, {}]}}},
                {"$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": {"$ifNull": ["$_stats.present_days", 0]}},
                    "late_count": {"$sum": {"$ifNull": ["$_stats.late_count", 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$_stats.total_late_minutes", 0]}},
                    "leave_count": {"$sum": {"$ifNull": ["$_stats.leave_count", 0]}},
                    "on_duty_count": {"$sum": {"$ifNull": ["$_stats.on_duty_count", 0]}},
                    "hours_sum": {"$sum": {"$ifNull": ["$_stats.hours_sum", 0]}},
                    "hours_count": {"$sum": {"$ifNull": ["$_stats.hours_count", 0]}},
                }},
                {"$sort": {"_id": 1}},
                {"$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": {"$cond": [
                        {"$gt": ["$hours_count", 0]},
                        {"$divide": ["$hours_sum", "$hours_count"]},
                        None,
                    ]},
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }},
            ],
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        month_start, month_end = month_bounds(require(month, "month"))
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                ]}},
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee",
            }},
            {"$unwind": "$_employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"_employee.department": department}})
        pipeline.extend([
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$_employee.name",
                "department": "$_employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])

        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    else:
        start_value = from_date if from_date is not None else date_from
        end_value = to_date if to_date is not None else date_to
        start_date = date_param(require(start_value, "from"), "from")
        end_date = date_param(require(end_value, "to"), "to")

        if start_date > end_date:
            raise HTTPException(422, "from must not be after to")
        if (
            datetime.strptime(end_date, "%Y-%m-%d").date()
            - datetime.strptime(start_date, "%Y-%m-%d").date()
        ).days > 91:
            raise HTTPException(422, "Date range must not exceed 92 days")
        dept = require(department, "department")
        if db.employees.find_one({"department": dept}) is None:
            raise HTTPException(404, "Department not found")

        # Same initial indexed date filter as the trend route; subsequent
        # densification/window stages do not alter the indexable match.
        collection = "attendance_logs"
        start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
        start_datetime = datetime.combine(start_day, datetime.min.time())
        end_datetime = datetime.combine(end_day, datetime.min.time())

        pipeline = [
            {"$match": {"date": {"$gte": start_date, "$lte": end_date}}},
            {"$lookup": {
                "from": "employees",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$emp_code", "$$code"]},
                        "department": dept,
                        "joined_on": {"$lte": end_date},
                    }}
                ],
                "as": "_employee",
            }},
            {"$unwind": "$_employee"},
            {"$set": {
                "_date": {"$dateFromString": {
                    "dateString": "$date",
                    "format": "%Y-%m-%d",
                }},
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_half": {"$ifNull": ["$half_day", False]},
            }},
            {"$group": {
                "_id": "$_date",
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": ["$_half", 0.5, 1.0]},
                    0,
                ]}},
                "late_count": {"$sum": {"$cond": [
                    {"$gt": ["$_late", 0]}, 1, 0,
                ]}},
            }},
            {"$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_datetime, end_datetime + timedelta(days=1)],
                },
            }},
            {"$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$and": [
                    {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                ]},
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"day": {"$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": "$_id",
                }}},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$lte": ["$joined_on", "$$day"]},
                        "department": dept,
                    }},
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }},
            {"$set": {
                "headcount": {"$ifNull": [
                    {"$arrayElemAt": ["$_headcount.count", 0]}, 0,
                ]},
            }},
            {"$set": {
                "attendance_rate": {"$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]},
            }},
            {"$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    },
                },
            }},
            {"$project": {
                "_id": 0,
                "date": {"$dateToString": {
                    "format": "%Y-%m-%d", "date": "$_id",
                }},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }},
            {"$sort": {"date": 1}},
        ]
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    try:
        explanation = db.command(
            "explain",
            command,
            verbosity="executionStats",
        )
    except PyMongoError as exc:
        raise HTTPException(
            500,
            f"MongoDB explain failed: {exc.__class__.__name__}",
        )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explanation)),
    }
