"""
Employee Attendance & Analytics API - Production Implementation
Contract: openapi.yaml (v2.0.0) | Data model: DATA_MODEL.md | Rules: R1 - R10

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, falling back to local .env)
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# MongoDB setup (must read from env; real env vars take priority)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Domain Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """R8: Rounding of reported numbers 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val)) if not isinstance(val, Decimal) else val
    factor = Decimal("10") ** (-decimals)
    return float(d.quantize(factor, rounding=ROUND_HALF_UP))


def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_millis(ms: int) -> datetime:
    """R1: Instants truncated to whole seconds before any calculation."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def now_truncated_dt() -> datetime:
    """Current UTC instant truncated to whole seconds."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=0)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight and punch_ist.strftime("%H:%M") < shift_end:
        attendance_date = punch_ist.date() - timedelta(days=1)
    else:
        attendance_date = punch_ist.date()
    return attendance_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(attendance_date_str).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    punch_ist = punch_in_dt.astimezone(IST)
    elapsed_seconds = (punch_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    val = Decimal(seconds) / Decimal(3600)
    return round_half_up(val, 2)


def compute_overtime(punch_out_dt: datetime, shift_end: str, shift_start: str, attendance_date_str: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if that is at least 30; otherwise 0.
    Overnight shifts: shift_end <= shift_start ends on the next calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    if shift_end <= shift_start:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_dt = datetime.fromisoformat(end_date.isoformat()).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    punch_ist = punch_out_dt.astimezone(IST)
    elapsed_seconds = (punch_ist - shift_end_dt).total_seconds()
    if elapsed_seconds >= 1800:
        return int(elapsed_seconds // 60)
    return 0


def serialize_employee_doc(doc: dict) -> dict:
    """Format employee document for HTTP response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_millis(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    """Format history entry for HTTP response."""
    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = to_epoch_millis(at_val)
    changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = to_epoch_millis(from_val)
            if isinstance(to_val, datetime):
                to_val = to_epoch_millis(to_val)
        changes[k] = {"from": from_val, "to": to_val}
    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    """Format attendance document for HTTP response (no internal _id or id)."""
    history_entries = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Request / Response Schemas
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts_and_date(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except Exception:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatusEnum = PresenceStatusEnum.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: 200 only when MongoDB answers ping, else 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create employee: unique emp_code enforced by unique index, returns 409 on duplicate."""
    doc = body.model_dump()
    doc["created_at"] = now_truncated_dt()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code asc, paginated, total reflects filters."""
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(e) for e in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Record punch-in: creates attendance record per R1 & R2.
    Race-safe: enforced by unique index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    punch_in_dt = from_epoch_millis(body.punched_at) if body.punched_at is not None else now_truncated_dt()
    d = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], d)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Record punch-out: closes most recent open record whose punch_in <= punched_at.
    Computes work hours (R4), overtime (R3), half-day (R5).
    Atomic update guarantees exactly one 200 on concurrent punch-outs.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    punch_out_dt = from_epoch_millis(body.punched_at) if body.punched_at is not None else now_truncated_dt()

    # Find the employee's most recent record whose punch_in is <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was concurrently punched out")

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records: sorted by date desc, emp_code asc.
    Uses index scan, avoids memory loading.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status.value
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize one day's record: recomputes derived fields and appends to audit history.
    Enforces R1-R5 and optimistic concurrency to prevent lost history entries.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="no record found for this date")

    # Setting status ABSENT or LEAVE clears both punch times; supplying punches is 422
    target_status = body.status.value if body.status is not None else record["status"]
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422, detail="supplying punch times with ABSENT or LEAVE is not allowed"
            )

    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            new_punch_in = from_epoch_millis(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            new_punch_out = from_epoch_millis(body.punch_out)
        else:
            new_punch_out = old_punch_out

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out, emp["shift_end"], emp["shift_start"], date
            )
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

    # Check for changes across all candidate fields
    changes = {}
    if target_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": target_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_entry = {
        "at": now_truncated_dt(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history_len = len(record.get("history", []))
    update_res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if update_res is None:
        raise HTTPException(status_code=409, detail="concurrent modification conflict")

    return serialize_attendance_doc(update_res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Aggregation Pipelines)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_weekday": {
                    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    "$is_weekday",
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee via aggregation pipeline.
    Working days computed from joined_on per R7.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_end = f"{month}-{num_days:02d}"

    # Calculate working days (Mon-Fri) from max(month_start, joined_on) to month_end
    working_days = 0
    if emp["joined_on"] <= month_end:
        start_date = max(date(year, m, 1), date.fromisoformat(emp["joined_on"]))
        end_date = date(year, m, num_days)
        curr = start_date
        while curr <= end_date:
            if curr.weekday() < 5:
                working_days += 1
            curr += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = data["present_days"]
        leave_days = data["leave_days"]
        late_count = data["late_count"]
        total_late = data["total_late_minutes"]
        total_ot = data["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$lte": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {"dateString": "$$l.date"}
                                                        }
                                                    },
                                                    5,
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            }
                        }
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                    "$$l.work_hours",
                                    0.0,
                                ]
                            }
                        }
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            }
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via MongoDB aggregation.
    Includes employees with zero logs in headcount; excludes people who join later.
    """
    pipeline = build_department_summary_pipeline(month, department)
    docs = list(db.employees.aggregate(pipeline))

    items = []
    for d in docs:
        avg_wh = None
        if d["work_hours_count"] > 0:
            avg_wh = round_half_up(d["work_hours_sum"] / d["work_hours_count"], 2)
        items.append({
            "department": d["_id"],
            "headcount": d["headcount"],
            "present_days": d["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"],
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month using standard competition ranking ($rank).
    Ties share rank, limit applies after ranking, sorted by total_late_minutes desc, emp_code asc.
    """
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    docs = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": docs}


def build_department_trend_pipeline(
    emp_codes: List[str], joined_on_dates: List[str], from_str: str, to_str: str
) -> List[Dict[str, Any]]:
    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    from_dt = datetime(from_d.year, from_d.month, from_d.day)
    to_dt_plus_1 = datetime(to_d.year, to_d.month, to_d.day) + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]},
            }
        },
        {
            "$set": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_on_dates,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$date"]},
                        }
                    }
                }
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
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                }
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily series with gap filling ($densify) and 7-day moving average ($setWindowFields).
    """
    try:
        f_d = date.fromisoformat(from_date)
        t_d = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(status_code=422, detail="invalid date format")

    if t_d < f_d:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")

    if (t_d - f_d).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")

    dept_employees = list(
        db.employees.find(
            {"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}
        )
    )
    if not dept_employees:
        raise HTTPException(status_code=404, detail="unknown department")

    emp_codes = [e["emp_code"] for e in dept_employees]
    joined_on_dates = sorted([e["joined_on"] for e in dept_employees])

    pipeline = build_department_trend_pipeline(emp_codes, joined_on_dates, from_date, to_date)
    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for d in docs:
        rate = round_half_up(d["attendance_rate"], 4) if d["attendance_rate"] is not None else None
        m_avg = round_half_up(d["moving_avg_7d"], 4) if d["moving_avg_7d"] is not None else None
        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": d["present_count"],
            "late_count": d["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    """
    Runs explain with executionStats verbosity for an endpoint's main query or pipeline.
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail="invalid explain endpoint")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status.value
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        exp = db.command("explain", command, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code required")
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipeline = build_department_summary_pipeline(month, department)
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department required")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' required")
        try:
            f_d = date.fromisoformat(from_date)
            t_d = date.fromisoformat(to_date)
        except Exception:
            raise HTTPException(status_code=422, detail="invalid date format")
        if t_d < f_d:
            raise HTTPException(status_code=422, detail="'to' must be >= 'from'")
        if (t_d - f_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="date range exceeds 92 days")

        dept_employees = list(
            db.employees.find(
                {"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}
            )
        )
        emp_codes = [e["emp_code"] for e in dept_employees]
        joined_on_dates = sorted([e["joined_on"] for e in dept_employees])
        pipeline = build_department_trend_pipeline(emp_codes, joined_on_dates, from_date, to_date)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }
