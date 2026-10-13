"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Consistent UTC-aware datetime strategy: PyMongo returns tz-aware UTC datetimes.
# Naive datetimes (legacy data / constructed values) are assumed to be UTC.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date"
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)], name="idx_emp_dept_code"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="idx_log_date_emp"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1), ("status", 1)],
        name="idx_log_emp_date_status",
    )
    db.attendance_logs.create_index(
        [("date", 1), ("emp_code", 1)], name="idx_log_date_emp_asc"
    )


EXPLAINABLE = ("attendance_list", "employee_monthly", "department_summary",
               "late_leaderboard", "department_trend")


try:
    ensure_indexes()
except Exception:
    pass

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    punch_in = ensure_utc(punch_in).replace(microsecond=0)
    local = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = (local - start).total_seconds()
    if diff_seconds <= 10 * 60:
        return 0
    return int(diff_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = ensure_utc(punch_in).replace(microsecond=0)
    punch_out = ensure_utc(punch_out).replace(microsecond=0)
    seconds = (punch_out - punch_in).total_seconds()
    return float((Decimal(str(seconds)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def shift_end_utc(date_str: str, shift_start: str, shift_end: str) -> datetime:
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    end_h, end_m = map(int, shift_end.split(":"))
    if shift_end <= shift_start:
        day = day + timedelta(days=1)
    return datetime(day.year, day.month, day.day, end_h, end_m, tzinfo=IST).astimezone(UTC)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str | None = None) -> int:
    punch_out = ensure_utc(punch_out).replace(microsecond=0)
    if shift_start is None:
        h, m = map(int, shift_end.split(":"))
        end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    else:
        end = shift_end_utc(date_str, shift_start, shift_end).astimezone(IST)
    diff_minutes = int((punch_out - end).total_seconds() // 60)
    if diff_minutes < 30:
        return 0
    return max(0, diff_minutes)


class EpochMillisError(ValueError):
    pass


def validate_epoch_ms(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EpochMillisError("epoch milliseconds must be an integer")
    if not (EPOCH_MS_MIN <= value <= EPOCH_MS_MAX):
        raise EpochMillisError("epoch milliseconds out of range")
    return value


def epoch_ms_to_utc(value: int) -> datetime:
    validate_epoch_ms(value)
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def utc_to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def now_utc_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ist_date_str(value: datetime, shift_start: str | None = None, shift_end: str | None = None) -> str:
    local = ensure_utc(value).astimezone(IST).replace(microsecond=0)
    day = local.date()
    if shift_start and shift_end and shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            day = day - timedelta(days=1)
    return day.isoformat()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
JOINED_ON_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EMPLOYEE_OUT_FIELDS = (
    "emp_code",
    "name",
    "email",
    "department",
    "shift_start",
    "shift_end",
    "joined_on",
    "created_at",
)


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": utc_to_epoch_ms(ensure_utc(doc["created_at"])),
    }


class EmployeeIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _check_joined_on(cls, value: str) -> str:
        if not JOINED_ON_RE.match(value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return value

    @model_validator(mode="after")
    def _check_shift_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: str = "PRESENT"

    @model_validator(mode="after")
    def _check(self):
        if self.status not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        if self.punched_at is not None:
            try:
                validate_epoch_ms(self.punched_at)
            except EpochMillisError as exc:
                raise ValueError(str(exc))
        return self


class PunchOutIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)

    @model_validator(mode="after")
    def _check(self):
        if self.punched_at is not None:
            try:
                validate_epoch_ms(self.punched_at)
            except EpochMillisError as exc:
                raise ValueError(str(exc))
        return self


ATTENDANCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PATCHABLE_FIELDS = ("status", "punch_in", "punch_out")
DERIVED_FIELDS = ("work_hours", "late_minutes", "overtime_minutes", "half_day")


def parse_calendar_date(value: str, field_name: str) -> str:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, f"{field_name} must be a valid calendar date")
    return value


def serialize_history_entry(entry: dict) -> dict:
    def conv(value):
        if isinstance(value, datetime):
            return utc_to_epoch_ms(ensure_utc(value))
        if isinstance(value, dict):
            return {k: conv(v) for k, v in value.items()}
        return value
    return {
        "at": conv(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {k: {"from": conv(v.get("from")), "to": conv(v.get("to"))} for k, v in (entry.get("changes") or {}).items()},
    }


def serialize_attendance(doc: dict) -> dict:
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    history = doc.get("history", [])
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_to_epoch_ms(ensure_utc(punch_in)) if punch_in is not None else None,
        "punch_out": utc_to_epoch_ms(ensure_utc(punch_out)) if punch_out is not None else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(e) for e in history],
    }


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "mongodb unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created_at = now_utc_truncated()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_at,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    d = ist_date_str(ts, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp.get("shift_start", "09:30")),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    cursor = (
        db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$lte": ts}})
        .sort("punch_in", -1)
        .limit(1)
    )
    docs = list(cursor)
    if not docs:
        raise HTTPException(404, "no punch-in found")
    target = docs[0]
    if target.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_ts = ensure_utc(target["punch_in"]).replace(microsecond=0)
    if ts <= punch_in_ts:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in_ts).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")
    work_hours = compute_work_hours(punch_in_ts, ts)
    overtime = compute_overtime(
        ts, emp.get("shift_end", "18:30"), target["date"], emp.get("shift_start", "09:30")
    )
    half_day = work_hours < 4.50
    res = db.attendance_logs.update_one(
        {"_id": target["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    updated = db.attendance_logs.find_one({"_id": target["_id"]}, {"_id": 0})
    if updated is None:
        raise HTTPException(404, "no punch-in found")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from is not None:
        parse_calendar_date(date_from, "date_from")
    if date_to is not None:
        parse_calendar_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if status is not None and status not in ATTENDANCE_STATUSES:
        raise HTTPException(422, "status must be one of PRESENT, WFH, ON_DUTY, ABSENT, LEAVE")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_q: dict = {}
        if date_from is not None:
            date_q["$gte"] = date_from
        if date_to is not None:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics helpers (R5-R9) — single definition; used by monthly/summary/board
# --------------------------------------------------------------------------- #
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6


def parse_month(value: str) -> tuple[int, int]:
    if not MONTH_RE.match(value):
        raise HTTPException(422, "month must be YYYY-MM")
    return int(value[:4]), int(value[5:7])


def month_bounds(year: int, mon: int) -> tuple[str, str]:
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def round2(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round4(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def working_weekdays(start: str, end: str) -> list[str]:
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    out: list[str] = []
    day = s
    while day <= e:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def iter_dates(start: str, end: str) -> list[str]:
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    out: list[str] = []
    day = s
    while day <= e:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def month_working_days(joined_on: str, first: str, last: str) -> int:
    start = max(joined_on, first)
    if start > last:
        return 0
    return len(working_weekdays(start, last))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    year, mon = parse_month(month)
    first, last = month_bounds(year, mon)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    joined_on: str = emp.get("joined_on", first)
    working_days = month_working_days(joined_on, first, last)
    start = max(joined_on, first)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": last}}},
        {"$addFields": {
            "is_present": {"$in": ["$status", list(PRESENT_STATUSES)]},
            "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
            "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            "is_half": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
            "is_leave": {"$eq": ["$status", "LEAVE"]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$is_present", "$is_weekday"]},
                {"$cond": ["$is_half", 0.5, 1.0]}, 0.0]}},
            "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
            "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    agg = rows[0] if rows else {}
    present_days = float(agg.get("present_days", 0.0) or 0.0)
    attendance_pct = round4(present_days / working_days * 100) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(agg.get("leave_days", 0) or 0),
        "late_count": int(agg.get("late_count", 0) or 0),
        "total_late_minutes": int(agg.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    year, mon = parse_month(month)
    first, last = month_bounds(year, mon)
    emp_match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        emp_match["department"] = department
    dept_rows = list(db.employees.aggregate([
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    "codes": {"$push": "$emp_code"}}},
    ]))
    dept_rows.sort(key=lambda r: r["_id"] or "")
    items: list[dict] = []
    for row in dept_rows:
        dept = row["_id"]
        codes: list = row.get("codes", [])
        if dept is None or not codes:
            continue
        log_rows = list(db.attendance_logs.aggregate([
            {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": first, "$lte": last}}},
            {"$addFields": {
                "is_present": {"$in": ["$status", list(PRESENT_STATUSES)]},
                "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                "is_half": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
            }},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [
                    {"$and": ["$is_present", "$is_weekday"]},
                    {"$cond": ["$is_half", 0.5, 1.0]}, 0.0]}},
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "wh_sum": {"$sum": {"$cond": [
                    {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                    "$work_hours", 0.0]}},
                "wh_n": {"$sum": {"$cond": [
                    {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                    1, 0]}},
            }},
        ]))
        agg = log_rows[0] if log_rows else {}
        n = int(agg.get("wh_n", 0) or 0)
        avg_wh = round2(agg["wh_sum"] / n) if n else None
        items.append({
            "department": dept,
            "headcount": int(row.get("headcount", 0)),
            "present_days": float(agg.get("present_days", 0.0) or 0.0),
            "avg_work_hours": avg_wh,
            "late_count": int(agg.get("late_count", 0) or 0),
            "total_late_minutes": int(agg.get("total_late_minutes", 0) or 0),
            "leave_count": int(agg.get("leave_count", 0) or 0),
            "on_duty_count": int(agg.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = parse_month(month)
    first, last = month_bounds(year, mon)
    info: dict[str, dict] = {}
    emp_filter: dict = {}
    if department is not None:
        emp_filter["department"] = department
    for doc in db.employees.find(emp_filter, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}):
        info[doc["emp_code"]] = {"name": doc.get("name", ""), "department": doc.get("department", "")}
    if department is not None and not info:
        return {"month": month, "items": []}
    pipeline: list[dict] = [{"$match": {"date": {"$gte": first, "$lte": last}}}]
    if info:
        pipeline.append({"$match": {"emp_code": {"$in": list(info.keys())}}})
    pipeline += [
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    if department is None:
        codes = [r["_id"] for r in rows]
        info = {}
        if codes:
            for doc in db.employees.find({"emp_code": {"$in": codes}}, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}):
                info[doc["emp_code"]] = {"name": doc.get("name", ""), "department": doc.get("department", "")}
        rows = [r for r in rows if r["_id"] in info]
    else:
        rows = [r for r in rows if r["_id"] in info]
    items: list[dict] = []
    prev_total = None
    rank = 0
    for idx, row in enumerate(rows, start=1):
        total = int(row.get("total_late_minutes", 0))
        if total != prev_total:
            rank = idx
            prev_total = total
        if rank > limit:
            break
        meta = info.get(row["_id"], {"name": "", "department": ""})
        items.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": meta["name"],
            "department": meta["department"],
            "total_late_minutes": total,
            "late_count": int(row.get("late_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(..., alias="to")):
    parse_calendar_date(from_, "from")
    parse_calendar_date(to, "to")
    if to < from_:
        raise HTTPException(422, "to must be on or after from")
    if (datetime.strptime(to, "%Y-%m-%d").date() - datetime.strptime(from_, "%Y-%m-%d").date()).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = [e.get("joined_on", from_) for e in emps]
    rows = list(db.attendance_logs.aggregate([
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to}}},
        {"$addFields": {
            "is_present": {"$in": ["$status", list(PRESENT_STATUSES)]},
            "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            "is_half": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                "$is_present", {"$cond": ["$is_half", 0.5, 1.0]}, 0.0]}},
            "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
        }},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day",
            "bounds": [
                datetime.strptime(from_, "%Y-%m-%d").replace(tzinfo=UTC),
                datetime.strptime(to, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1),
            ]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"day": 1}},
    ]))
    by_day = {r["date"]: r for r in rows}
    items: list[dict] = []
    rates: list[Optional[float]] = []
    for r in rows:
        day = r["date"]
        weekday = datetime.strptime(day, "%Y-%m-%d").weekday() < 5
        headcount = sum(1 for j in joined if j <= day)
        agg = by_day.get(day, {})
        present = float(agg.get("present_count", 0.0) or 0.0)
        rate = round4(present / headcount) if weekday and headcount else None
        rates.append(rate)
        window = [v for v in rates[max(0, len(rates) - 7):] if v is not None]
        moving = round4(sum(window) / len(window)) if window else None
        items.append({
            "date": day,
            "is_working_day": weekday,
            "headcount": headcount,
            "present_count": present,
            "late_count": int(agg.get("late_count", 0) or 0),
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin explain (see openapi.yaml):
# --------------------------------------------------------------------------- #

def _explain_find(collection: str, filt: dict, sort, skip: int, limit: int) -> dict:
    cmd = {"explain": {"find": collection, "filter": filt,
                       "sort": sort, "skip": skip, "limit": limit},
           "verbosity": "executionStats"}
    return db.command(cmd)


def _explain_agg(collection: str, pipeline: list) -> dict:
    cmd = {"explain": {"aggregate": collection, "pipeline": pipeline,
                       "cursor": {}}, "verbosity": "executionStats"}
    return db.command(cmd)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    if endpoint not in EXPLAINABLE:
        raise HTTPException(422, "unknown explain endpoint")
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "invalid pagination")
    if limit < 1 or limit > 50:
        raise HTTPException(422, "invalid limit")
    if month is not None:
        parse_month(month)
    for name, value in (("date_from", date_from), ("date_to", date_to),
                        ("from", from_), ("to", to)):
        if value is not None:
            parse_calendar_date(value, name)
    if status is not None and status not in ATTENDANCE_STATUSES:
        raise HTTPException(422, "invalid status")
    try:
        if endpoint == "attendance_list":
            if date_from is not None and date_to is not None and date_from > date_to:
                raise HTTPException(422, "date_from must be on or before date_to")
            q: dict = {}
            if emp_code is not None:
                q["emp_code"] = emp_code
            if date_from is not None or date_to is not None:
                date_q: dict = {}
                if date_from is not None:
                    date_q["$gte"] = date_from
                if date_to is not None:
                    date_q["$lte"] = date_to
                q["date"] = date_q
            if status is not None:
                q["status"] = status
            out = _explain_find("attendance_logs", q,
                                {"date": -1, "emp_code": 1},
                                (page - 1) * page_size, page_size)
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
        if endpoint == "employee_monthly":
            if emp_code is None or month is None:
                raise HTTPException(422, "emp_code and month are required")
            y, mo = parse_month(month)
            first, last = month_bounds(y, mo)
            pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": "$emp_code",
                                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
                    {"$sort": {"_id": 1}}]
            return {"endpoint": endpoint, "collection": "attendance_logs",
                    "explain": _explain_agg("attendance_logs", pipe)}
        if endpoint == "department_summary":
            if month is None:
                raise HTTPException(422, "month is required")
            y, mo = parse_month(month)
            first, last = month_bounds(y, mo)
            codes = [d["emp_code"] for d in db.employees.find(
                {"department": department, "joined_on": {"$lte": last}} if department is not None else {},
                {"_id": 0, "emp_code": 1})]
            if not codes:
                codes = ["__no_such_employee__"]
            pipe = [{"$match": {"emp_code": {"$in": codes}, "date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": None,
                                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
                    {"$sort": {"_id": 1}}]
            return {"endpoint": endpoint, "collection": "attendance_logs",
                    "explain": _explain_agg("attendance_logs", pipe)}
        if endpoint == "late_leaderboard":
            if month is None:
                raise HTTPException(422, "month is required")
            y, mo = parse_month(month)
            first, last = month_bounds(y, mo)
            match: dict = {"date": {"$gte": first, "$lte": last}}
            if department is not None:
                codes = [d["emp_code"] for d in db.employees.find(
                    {"department": department}, {"_id": 0, "emp_code": 1})]
                match["emp_code"] = {"$in": codes}
            pipe = [{"$match": match},
                    {"$group": {"_id": "$emp_code",
                                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
                    {"$sort": {"total_late_minutes": -1, "_id": 1}}]
            return {"endpoint": endpoint, "collection": "attendance_logs",
                    "explain": _explain_agg("attendance_logs", pipe)}
        if department is None or from_ is None or to is None:
            raise HTTPException(422, "department, from and to are required")
        if to < from_:
            raise HTTPException(422, "to must be on or after from")
        codes = [d["emp_code"] for d in db.employees.find(
            {"department": department}, {"_id": 0, "emp_code": 1})]
        pipe = [{"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to}}},
                {"$group": {"_id": "$date",
                            "present_count": {"$sum": 1}}},
                {"$sort": {"_id": 1}}]
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain_agg("attendance_logs", pipe)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"explain failed: {exc}")


class RegularizeIn(BaseModel):
    model_config = {"extra": "ignore"}

    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    regularized_by: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=5, max_length=200)

    @model_validator(mode="after")
    def _check(self):
        if self.status is not None and self.status not in ATTENDANCE_STATUSES:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY, ABSENT, LEAVE")
        for name in ("punch_in", "punch_out"):
            value = getattr(self, name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{name} must be epoch milliseconds")
                try:
                    validate_epoch_ms(value)
                except EpochMillisError as exc:
                    raise ValueError(str(exc))
        return self


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_calendar_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    raw = body.model_dump()
    present = {k: v for k, v in raw.items() if k in PATCHABLE_FIELDS and v is not None}
    if not present:
        raise HTTPException(422, "at least one of status, punch_in, punch_out must be provided")
    shift_start = current.get("shift_start", emp.get("shift_start", "09:30"))
    shift_end = current.get("shift_end", emp.get("shift_end", "18:30"))
    new_status = present.get("status", current.get("status"))
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = epoch_ms_to_utc(present["punch_in"]) if "punch_in" in present else (
            ensure_utc(current["punch_in"]).replace(microsecond=0) if current.get("punch_in") is not None else None
        )
        new_punch_out = epoch_ms_to_utc(present["punch_out"]) if "punch_out" in present else (
            ensure_utc(current["punch_out"]).replace(microsecond=0) if current.get("punch_out") is not None else None
        )
        if new_punch_in is None:
            raise HTTPException(422, "punch_in is required for present statuses")
        if new_punch_out is not None and new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if new_punch_out is not None and (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
        if "punch_in" in present and ist_date_str(new_punch_in, shift_start, shift_end) != date:
            raise HTTPException(422, "punch_in must fall on the attendance date")
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late = 0
        new_overtime = 0
        new_half = False
    elif new_punch_out is None:
        new_work_hours = None
        new_late = compute_late_minutes(new_punch_in, shift_start)
        new_overtime = 0
        new_half = False
    else:
        new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
        new_late = compute_late_minutes(new_punch_in, shift_start)
        new_overtime = compute_overtime(new_punch_out, shift_end, date, shift_start)
        new_half = new_work_hours < 4.50
    candidate = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half,
    }
    changes: dict = {}
    for field, new_value in candidate.items():
        old_value = current.get(field)
        if isinstance(old_value, datetime):
            old_value = ensure_utc(old_value).replace(microsecond=0)
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        return serialize_attendance({**current, "history": current.get("history", [])})
    entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    res = db.attendance_logs.update_one(
        {"_id": current["_id"]},
        {"$set": candidate, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "attendance record not found")
    updated = db.attendance_logs.find_one({"_id": current["_id"]}, {"_id": 0})
    if updated is None:
        raise HTTPException(404, "attendance record not found")
    return serialize_attendance(updated)
