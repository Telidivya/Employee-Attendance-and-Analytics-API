"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded, but real environment variables win)

Layout of this single file
  1. configuration and Mongo client
  2. pure business-rule helpers (R1 - R8)  - no I/O, unit-testable
  3. request models and response serialisation
  4. index creation (run at startup)
  5. pipeline / query builders (shared by the real endpoints AND /admin/explain)
  6. endpoints
"""
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: BSON datetimes come back as aware UTC datetimes, never naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

# --------------------------------------------------------------------------- #
# 2. Pure business-rule helpers  (no I/O below this line until the end marker)
# --------------------------------------------------------------------------- #
# BEGIN PURE HELPERS
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")
LATE_GRACE_SECONDS = 600  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
ONE_SECOND = timedelta(seconds=1)
ONE_MINUTE = timedelta(minutes=1)


def utc_now_ms() -> datetime:
    """Server clock, truncated to milliseconds (BSON precision)."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime -> integer epoch milliseconds (exact integer arithmetic, no floats)."""
    if dt is None:
        return None
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(value: str) -> Time:
    h, m = value.split(":")
    return Time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; for overnight shifts a punch-in before
    shift_end belongs to the shift that started the previous day."""
    ist = aware(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_at(att_date: Date, shift_start: str) -> datetime:
    return datetime.combine(att_date, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_at(att_date: Date, shift_start: str, shift_end: str) -> datetime:
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    return datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: Date) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then floor minutes
    since shift start (not since the end of the grace period)."""
    secs = (aware(punch_in) - shift_start_at(att_date, shift_start)) // ONE_SECOND
    return secs // 60 if secs > LATE_GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: Date) -> int:
    """R3: floor minutes from shift end to punch-out, counted only if >= 30."""
    secs = (aware(punch_out) - shift_end_at(att_date, shift_start, shift_end)) // ONE_SECOND
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, so no float/banker's rounding)."""
    secs = (aware(punch_out) - aware(punch_in)) // ONE_SECOND
    hours = (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def is_half_day(work_hours: float) -> bool:
    """R5: decided on the ROUNDED work_hours."""
    return Decimal(str(work_hours)) < HALF_DAY_LIMIT


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  shift_start: str, shift_end: str, att_date: Date) -> dict:
    """The four server-computed fields, per the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    punch_in = aware(punch_in).replace(microsecond=0)
    out["late_minutes"] = compute_late_minutes(punch_in, shift_start, att_date)
    if punch_out is not None:
        punch_out = aware(punch_out).replace(microsecond=0)
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, shift_start, shift_end, att_date)
        out["half_day"] = is_half_day(out["work_hours"])
    return out


def half_up(value: Any, places: int) -> float:
    """R8 rounding. Floats are first cleaned of binary noise, then rounded half-up."""
    d = value if isinstance(value, Decimal) else Decimal(str(round(float(value), 10)))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple:
    """'2026-07' -> (date(2026,7,1), date(2026,7,31)). Raises ValueError for e.g. year 0000."""
    year, mon = int(month[:4]), int(month[5:7])
    first = Date(year, mon, 1)
    nxt = Date(year + (mon == 12), mon % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def count_weekdays(first: Date, last: Date) -> int:
    """Mon-Fri days in [first, last] (R7); at most 31 iterations for a month."""
    n, day = 0, first
    while day <= last:
        n += day.weekday() < 5
        day += timedelta(days=1)
    return n


def weekend_dates(first: Date, last: Date) -> list:
    out, day = [], first
    while day <= last:
        if day.weekday() >= 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out
# END PURE HELPERS


# --------------------------------------------------------------------------- #
# 3. Models and serialisation
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# StrictInt: rejects floats ("1.5", 1.0), strings and booleans; the range check rejects
# values that look like seconds (< 1e11) or are absurdly large.
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]

_YMD = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # created_at etc. from a client are ignored

    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=_HHMM)
    shift_end: str = Field(default="18:30", pattern=_HHMM)
    joined_on: str = Field(pattern=_YMD)

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        Date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields sent by a client are silently ignored (extra="ignore").
    # The optional fields default to None WITHOUT being Optional[...], so an explicit
    # JSON null is a 422 (the contract has no nullable fields here).
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def unprocessable(loc: str, msg: str, where: str = "query"):
    """Raise a 422 with FastAPI's standard body shape."""
    raise RequestValidationError([{"type": "value_error", "loc": (where, loc), "msg": msg, "input": None}])


def parse_ymd(value: str, loc: str, where: str = "query") -> Date:
    if not re.fullmatch(_YMD, value):
        unprocessable(loc, "must be a date in YYYY-MM-DD format", where)
    try:
        return Date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date", where)


def parse_month(month: str) -> tuple:
    try:
        return month_bounds(month)
    except ValueError:
        unprocessable("month", "not a valid month")


def _json_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return to_ms(v)
    if isinstance(v, dict):
        return {k: _json_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json_value(x) for x in v]
    return v


def record_out(doc: dict) -> dict:
    """Mongo document -> API shape. Never leaks _id; tolerates legacy documents."""
    history = [
        {"at": to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"),
         "changes": _json_value(e.get("changes") or {})}
        for e in (doc.get("history") or [])
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_idx_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> bool:
    global _indexes_ready
    if _indexes_ready:
        return True
    with _idx_lock:
        if _indexes_ready:
            return True
        try:
            emp, logs = db.employees, db.attendance_logs
            # employees
            emp.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
            emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
            emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
            emp.create_index([("joined_on", ASCENDING)], name="joined_on")
            # attendance_logs
            logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_code_date")
            logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
            logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                              name="status_date_emp_code")
            logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in")
            logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING), ("late_minutes", ASCENDING)],
                              name="late_only", partialFilterExpression={"late_minutes": {"$gt": 0}})
            _indexes_ready = True
        except PyMongoError as exc:  # Mongo not reachable yet: /health will report it
            log.warning("index creation failed: %s", exc)
        return _indexes_ready


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def _mongo_error(_, exc: PyMongoError):
    log.error("mongo error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 5. Query / pipeline builders  (shared by endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_find(emp_code, date_from, date_to, status, page, page_size) -> dict:
    df = parse_ymd(date_from, "date_from") if date_from else None
    dt = parse_ymd(date_to, "date_to") if date_to else None
    if df and dt and df > dt:
        unprocessable("date_from", "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status:
        q["status"] = status
    # With emp_code fixed, the emp_code tie-breaker is meaningless, and dropping it lets the
    # unique (emp_code, date) index serve the sort in reverse.
    sort = {"date": -1} if emp_code else {"date": -1, "emp_code": 1}
    return {"filter": q, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size,
            "projection": {"_id": 0}}


def _log_exprs(p: str, weekend: list) -> dict:
    """Aggregation expressions over one attendance record; `p` is the field prefix
    ('$' for top-level fields, '$logs.' after an $unwind of a $lookup)."""
    status = {"$ifNull": [p + "status", ""]}
    is_present = {"$in": [status, list(PRESENCE)]}
    half = {"$eq": [{"$ifNull": [p + "half_day", False]}, True]}
    weight = {"$cond": [is_present, {"$cond": [half, 0.5, 1]}, 0]}  # R5 / R6
    late = {"$ifNull": [p + "late_minutes", 0]}
    has_hours = {"$and": [is_present, {"$isNumber": p + "work_hours"}]}
    return {
        "weight": weight,
        "weekday_weight": {"$cond": [{"$in": [{"$ifNull": [p + "date", ""]}, weekend]}, 0, weight]},  # R7
        "late_flag": {"$cond": [{"$gt": [late, 0]}, 1, 0]},
        "late_min": late,
        "overtime": {"$ifNull": [p + "overtime_minutes", 0]},
        "is_leave": {"$cond": [{"$eq": [status, "LEAVE"]}, 1, 0]},
        "is_on_duty": {"$cond": [{"$eq": [status, "ON_DUTY"]}, 1, 0]},
        "work_cents": {"$cond": [has_hours, {"$round": [{"$multiply": [p + "work_hours", 100]}, 0]}, 0]},
        "work_flag": {"$cond": [has_hours, 1, 0]},
    }


def build_employee_monthly(emp_code: str, month: str) -> tuple:
    first, last = parse_month(month)
    e = _log_exprs("$", weekend_dates(first, last))
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": e["weekday_weight"]},
            "leave_days": {"$sum": e["is_leave"]},
            "late_count": {"$sum": e["late_flag"]},
            "total_late_minutes": {"$sum": e["late_min"]},
            "total_overtime_minutes": {"$sum": e["overtime"]},
        }},
    ]
    return "attendance_logs", pipeline


def build_department_summary(month: str, department: Optional[str]) -> tuple:
    first, last = parse_month(month)
    e = _log_exprs("$logs.", weekend_dates(first, last))
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: no logs needed to be counted
    if department:
        match["department"] = department
    pipeline = [
        {"$match": match},
        # Base the pipeline on employees so people with zero logs survive; the correlated
        # $lookup uses the (emp_code, date) index for every employee.
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1,
                              "late_minutes": 1}},
            ],
            "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # one row per employee ...
        {"$group": {
            "_id": "$emp_code", "department": {"$first": "$department"},
            "present_days": {"$sum": e["weekday_weight"]},
            "work_cents": {"$sum": e["work_cents"]}, "work_count": {"$sum": e["work_flag"]},
            "late_count": {"$sum": e["late_flag"]}, "total_late_minutes": {"$sum": e["late_min"]},
            "leave_count": {"$sum": e["is_leave"]}, "on_duty_count": {"$sum": e["is_on_duty"]},
        }},
        # ... then one row per department (average over RECORDS: sum / count, not mean of means)
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_cents": {"$sum": "$work_cents"}, "work_count": {"$sum": "$work_count"},
            "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> tuple:
    first, last = parse_month(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). The ranking happens BEFORE the limit.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return "attendance_logs", pipeline


def build_trend(department: str, first: Date, last: Date) -> tuple:
    n_days = (last - first).days + 1
    start_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)
    e = _log_exprs("$logs.", [])
    pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": last.isoformat()}}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs"}},
        {"$project": {"_id": 0, "joined_on": 1, "logs": 1}},
        # $facet always emits exactly one document, even when the department has nobody yet.
        {"$facet": {
            "joined": [{"$group": {"_id": None, "dates": {"$push": "$joined_on"}}}],
            "daily": [{"$unwind": "$logs"},
                      {"$group": {"_id": "$logs.date", "present": {"$sum": e["weight"]},
                                  "late": {"$sum": e["late_flag"]}}}],
        }},
        {"$project": {
            "joined": {"$ifNull": [{"$arrayElemAt": ["$joined.dates", 0]}, []]},
            "daily": 1,
            # gap filling: one element per calendar day of the range, generated in the database
            "days": {"$map": {
                "input": {"$range": [0, n_days]}, "as": "i",
                "in": {"$let": {
                    "vars": {"dt": {"$add": [start_dt, {"$multiply": ["$$i", 86400000]}]}},
                    "in": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}},
                           "dow": {"$dayOfWeek": "$$dt"}}}}}},
        }},
        {"$unwind": "$days"},
        {"$set": {
            "date": "$days.date",
            "is_working_day": {"$and": [{"$gte": ["$days.dow", 2]}, {"$lte": ["$days.dow", 6]}]},  # Mon-Fri
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j",
                                                "cond": {"$lte": ["$$j", "$days.date"]}}}},
            "stat": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "x",
                                                   "cond": {"$eq": ["$$x._id", "$days.date"]}}}, 0]},
        }},
        {"$set": {"present_count": {"$ifNull": ["$stat.present", 0]},
                  "late_count": {"$ifNull": ["$stat.late", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # window over the rows of the requested range only; $avg skips null rates
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "MongoDB is not reachable"})
    ensure_indexes()  # no-op once done; retried here if Mongo was down at startup
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now_ms()
    try:  # uniqueness is enforced by the unique index, not by a read-then-write
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = (db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
           .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# ---- attendance ----------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": att_date.isoformat(), "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive_fields(body.status, ts, None, emp["shift_start"], emp["shift_end"], att_date),
        "history": [],
    }
    try:  # the unique (emp_code, date) index decides the race: one insert wins, the rest 409
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"]).replace(microsecond=0)
    if ts <= p_in:
        unprocessable("punched_at", "punched_at must be after the punch-in", "body")
    if ts - p_in > MAX_SHIFT:
        unprocessable("punched_at", "punch-out is more than 24 hours after the punch-in", "body")
    derived = derive_fields(rec["status"], p_in, ts, emp["shift_start"], emp["shift_end"],
                            Date.fromisoformat(rec["date"]))
    # Atomic compare-and-set: only the request that still sees an open record wins.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    q = spec["filter"]
    # R10: total must be the exact count matching the applied filters, including no filters.
    total = db.attendance_logs.count_documents(q)
    cur = (db.attendance_logs.find(q, spec["projection"]).sort(list(spec["sort"].items()))
           .skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_day = parse_ymd(date, "date", "path")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_day.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    cur_status, cur_in, cur_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    sent_in, sent_out = body.punch_in is not None, body.punch_out is not None

    # ---- the final state after the correction ---------------------------------------------
    if body.status in NON_PRESENCE:
        if sent_in or sent_out:
            unprocessable("punch_in", "ABSENT/LEAVE clear the punch times; do not send them", "body")
        new_status, new_in, new_out = body.status, None, None
    else:
        new_status = body.status or cur_status
        if new_status in NON_PRESENCE:  # record is ABSENT/LEAVE and no presence status was given
            if sent_in or sent_out:
                unprocessable("status", "set a presence status to add punch times", "body")
            new_in = new_out = None
        else:
            new_in = from_ms(body.punch_in) if sent_in else cur_in
            new_out = from_ms(body.punch_out) if sent_out else cur_out
            if new_in is None:
                unprocessable("punch_in", "a presence status requires a punch_in", "body")

    # ---- validation (R1 and the 24 h rule) -------------------------------------------------
    if sent_in and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != att_day:
        unprocessable("punch_in", "punch_in must stay on the record's attendance date", "body")
    if new_in is not None and new_out is not None and (sent_in or sent_out):
        gap = aware(new_out).replace(microsecond=0) - aware(new_in).replace(microsecond=0)
        if gap <= timedelta(0) or gap > MAX_SHIFT:
            unprocessable("punch_out", "punch_out must be after punch_in and within 24 hours", "body")

    # ---- recompute the derived fields and diff against what is stored -------------------------
    derived = derive_fields(new_status, new_in, new_out, emp["shift_start"], emp["shift_end"], att_day)
    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old_vals = {"status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
                "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
                "overtime_minutes": rec.get("overtime_minutes") or 0,
                "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if new_vals[k] != old_vals[k]}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        unprocessable("body", "the request changes nothing", "body")

    entry = {"at": utc_now_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_history = len(rec.get("history") or [])
    # Optimistic concurrency: the write only applies if the record is still exactly as we read it
    # (same history length, same status and punch times). Otherwise somebody else corrected it
    # first, and we answer 409 instead of silently overwriting their history entry.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
         "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_history]}},
        {"$set": new_vals, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return record_out(updated)


# ---- analytics ------------------------------------------------------------ #
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = parse_month(month)
    emp = get_employee_or_404(emp_code)
    start = max(first, Date.fromisoformat(emp["joined_on"]))  # R7: count from joined_on
    working_days = count_weekdays(start, last) if start <= last else 0
    coll, pipeline = build_employee_monthly(emp_code, month)
    rows = list(db[coll].aggregate(pipeline))
    r = rows[0] if rows else {}
    present = r.get("present_days", 0)
    pct = half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": half_up(present, 2), "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)), "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    coll, pipeline = build_department_summary(month, department)
    items = []
    for r in db[coll].aggregate(pipeline, allowDiskUse=True):
        cnt = int(r["work_count"])
        avg = half_up(Decimal(int(r["work_cents"])) / (Decimal(cnt) * 100), 2) if cnt else None
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]),
            "present_days": half_up(r["present_days"], 2), "avg_work_hours": avg,
            "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    coll, pipeline = build_late_leaderboard(month, limit, department)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
              "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
              "late_count": int(r["late_count"])} for r in db[coll].aggregate(pipeline, allowDiskUse=True)]
    return {"month": month, "items": items}


def _trend_args(department: Optional[str], from_: Optional[str], to: Optional[str]) -> tuple:
    if not department:
        unprocessable("department", "field required")
    if not from_:
        unprocessable("from", "field required")
    if not to:
        unprocessable("to", "field required")
    first, last = parse_ymd(from_, "from"), parse_ymd(to, "to")
    if last < first:
        unprocessable("to", "to must not be before from")
    if (last - first).days + 1 > 92:
        unprocessable("to", "the range must not exceed 92 days")
    return first, last


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    first, last = _trend_args(department, from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    coll, pipeline = build_trend(department, first, last)
    items = []
    for r in db[coll].aggregate(pipeline, allowDiskUse=True):
        hc, pc = int(r["headcount"]), r["present_count"]
        rate = r.get("attendance_rate")
        mov = r.get("moving_avg_7d")
        items.append({
            "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": hc,
            "present_count": half_up(pc, 2), "late_count": int(r["late_count"]),
            # recomputed from exact numbers so the 4-decimal rounding is truly half-up
            "attendance_rate": None if rate is None else half_up(Decimal(str(pc)) / Decimal(hc), 4),
            "moving_avg_7d": None if mov is None else half_up(mov, 4),
        })
    return {"department": department, "items": items}


# ---- admin: explain ------------------------------------------------------- #
class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs explain('executionStats') on the SAME query/pipeline the real endpoint builds."""
    name = endpoint.value
    if name == "attendance_list":
        spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
        coll, cmd = "attendance_logs", {"find": "attendance_logs", **spec}
    else:
        if name == "employee_monthly":
            if not emp_code:
                unprocessable("emp_code", "field required")
            if not month:
                unprocessable("month", "field required")
            coll, pipeline = build_employee_monthly(emp_code, month)
        elif name == "department_summary":
            if not month:
                unprocessable("month", "field required")
            coll, pipeline = build_department_summary(month, department)
        elif name == "late_leaderboard":
            if not month:
                unprocessable("month", "field required")
            coll, pipeline = build_late_leaderboard(month, limit, department)
        else:  # department_trend
            first, last = _trend_args(department, from_, to)
            coll, pipeline = build_trend(department, first, last)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    explain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": name, "collection": coll, "explain": explain}
