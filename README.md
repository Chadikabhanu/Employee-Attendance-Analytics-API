# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for employee attendance tracking, automated policy calculations (late minutes, work hours, overtime, half-days), audit logging, and organizational analytics.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (requires `$densify` and `$setWindowFields` support)

## Getting Started

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment** (defaults to local MongoDB):
   ```bash
   # Optional overrides:
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Start the API service**:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

4. **Interactive Documentation**:
   - Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
   - ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

Database indexes are created automatically during application startup via FastAPI's lifespan event.

---

## Architecture & Implementation Details

- **Single Module Structure**: All models, business logic helpers, and endpoint routers are organized inside [`app/main.py`](file:///c:/Users/Mouni/Downloads/SDT_candidate_kit/app/main.py).
- **Timezone & Shift Calculation**:
  - All shift rules and attendance dates operate strictly in IST (`UTC+05:30`).
  - Overnight shifts (`shift_end <= shift_start`) are handled by attributing punch-ins occurring before `shift_end` the following morning to the previous day's shift date.
- **Rounding Accuracy**:
  - Uses Python's `decimal` module configured with `ROUND_HALF_UP` to prevent Banker's rounding bias on work hours (2 decimals) and attendance rates (4 decimals).
- **Concurrency & Data Integrity**:
  - **Punch-In**: Guaranteed race-safe via a unique compound index on `(emp_code, date)`. Duplicate requests throw `DuplicateKeyError`, handled as `409 Conflict`.
  - **Punch-Out**: Atomic state transition using `find_one_and_update` on open records (`punch_out: None`), preventing race conditions.
  - **Regularization**: Employs optimistic locking on the attendance audit trail by asserting the array size of `history` before committing updates.
- **Analytics Aggregations**:
  - Implemented entirely within MongoDB aggregation pipelines rather than in-memory Python iteration.
  - Department headcount aggregates starting from `employees` with `$lookup` to properly retain employees with zero logs.
  - Late leaderboard handles score ties via MongoDB `$setWindowFields` with standard competition ranking (`$rank`).
  - Department trend utilizes `$densify` for calendar gap-filling and rolling windows for 7-day moving averages.
- **Query Optimization (`/admin/explain/{endpoint}`)**:
  - Provides execution plan metrics (`executionStats`) verifying index scan coverage (`IXSCAN`) across key analytical and transactional queries.
