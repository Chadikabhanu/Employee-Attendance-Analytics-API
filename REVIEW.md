# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Input timestamp used `.replace()` without converting from UTC to IST, causing incorrect negative time differences. | Punch in at 10:05 AM IST (04:35 UTC) against a 09:30 AM shift; returned `0` instead of `35`. | Explicitly converted input instant to IST (`UTC+05:30`) prior to evaluating shift start delta. |
| 2 | `compute_late_minutes` | Grace period threshold performed integer division `int(seconds / 60) > 10`. At 10m 01s, `int(601/60)` is 10, incorrectly passing grace check. | Punch in at 09:40:01 for a 09:30 shift; returned `0` instead of `10`. | Evaluated raw seconds against strict grace boundary (`total_seconds > 600`) before integer division. |
| 3 | `compute_late_minutes` | Overnight shifts (`shift_end <= shift_start`) defaulted shift start to the current day even when punching in post-midnight. | Punch in at 00:05 for a 22:00–06:00 shift; evaluated start 22 hours in future, yielding 0 late minutes. | Checked overnight shift condition; attributed shift start to previous calendar date when punching in after midnight. |
| 4 | `compute_work_hours` | Native `round()` used Banker's rounding (round-half-to-even), violating business rule R4. | Duration of 2.125 hours rounded down to 2.12 instead of 2.13. | Replaced with Python `decimal.Decimal` utilizing `ROUND_HALF_UP` to 2 decimal places. |
| 5 | `compute_overtime` | Overtime accrued immediately past shift end without enforcing the mandatory 30-minute threshold (R3). | Punch out 5 minutes after shift end; awarded 5 minutes overtime instead of 0. | Added qualifying guard `elapsed_seconds >= 1800` (30 mins); returns 0 otherwise. |
| 6 | `compute_overtime` | Overnight shift end datetime assumed same calendar day as shift start date. | Punch out at 06:40 for a 22:00–06:00 shift; computed 1,400+ minutes of overtime. | Added 1 day to shift end date when `shift_end <= shift_start`. |
| 7 | `health` | Readiness probe returned static `{"status": "ok"}` without testing database connectivity. | Terminating MongoDB still yielded HTTP 200. | Integrated `db.command("ping")` within try-except; returns HTTP 503 on database unavailability. |
| 8 | `create_employee` | Performed `find_one()` followed by `insert_one()`, allowing duplicate creation under race conditions. | Concurrent requests with identical `emp_code` created duplicates or triggered unhandled 500s. | Backed by unique index on `emp_code` and handled `DuplicateKeyError` to return HTTP 409. |
| 9 | `create_employee` | Pydantic schema lacked validation for identical shift boundaries, email format, and code format. | Payload with `shift_start: "09:30"` and `shift_end: "09:30"` was accepted. | Added regex validators and model validator ensuring `shift_start != shift_end` and ISO date validity. |
| 10 | `create_employee` & `list_employees` | Emitted `created_at` as ISO string or datetime object instead of epoch milliseconds per OpenAPI spec. | Response contained string timestamp rather than integer millisecond timestamp. | Converted datetime to epoch milliseconds via `.timestamp() * 1000`. |
| 11 | `list_employees` | Pagination offset calculated as `skip = page * page_size`, skipping the entire first page. | Requesting `/employees?page=1` omitted records 1–20. | Corrected offset logic to `skip = (page - 1) * page_size`. |
| 12 | `list_employees` | Total record count executed `count_documents({})`, disregarding active department filters. | Department-filtered listing returned total company employee count. | Passed filter query `q` into `count_documents(q)`. |
| 13 | `list_employees` | Missing deterministic sort order; records returned in natural order. | Employees listed inconsistently across requests. | Added explicit database sort on `emp_code` ascending. |
| 14 | `punch_in` | Unchecked employee lookup resulted in `None["shift_start"]`, throwing unhandled `TypeError` (500). | Punch in with non-existent `emp_code` threw HTTP 500. | Added explicit validation raising HTTP 404 if employee document is absent. |
| 15 | `punch_in` | Read-then-write check permitted duplicate punch-ins on concurrent requests. | Concurrent punches for same employee on same date produced duplicates. | Enforced unique compound index on `(emp_code, date)` and caught `DuplicateKeyError` to return HTTP 409. |
| 16 | `punch_in` | Attendance date derived from host server local timezone instead of IST (+05:30) and ignored overnight shifts. | Punching in at 01:00 for night shift tagged today's date instead of shift start date. | Converted punch instant to IST, verified overnight condition, and shifted date back by 1 day when applicable. |
| 17 | `punch_in` | Failed to truncate timestamps to whole seconds before processing (R1). | Stored timestamps retained fractional sub-second milliseconds. | Truncated milliseconds to zero before calculation and database insertion. |
| 18 | `punch_in` | Response payload leaked internal `id` / `_id` and raw datetime format. | Response violated OpenAPI schema requirements. | Normalized serializer to emit epoch milliseconds and excluded internal identifiers. |
| 19 | `list_attendance` | Loaded entire collection into memory via `list(find())` and sorted in Python. | High memory footprint and latency degradation on moderate dataset sizes. | Moved sorting (`date` DESC, `emp_code` ASC) and pagination directly into indexed database cursor operations. |
| 20 | `list_attendance` | Sorted exclusively by `date` descending, leaving same-day records non-deterministic. | Multiple records on same date exhibited arbitrary ordering. | Added secondary sort key `emp_code` ascending. |
| 21 | `list_attendance` | Lacked validation on date range bounds. | Passing `date_from > date_to` returned 200 with an empty array. | Added input validation raising HTTP 422 if `date_from > date_to`. |
| 22 | Application startup | Indexes were never registered; collections ran unindexed with full table scans. | Query explain plans revealed `COLLSCAN` across all queries. | Registered all required unique and compound indexes inside FastAPI `lifespan` handler. |

---

## Architectural Validations (Confirmed Non-Defects)

1. **`emp_code` as String Identifier**:
   `emp_code` is the domain natural key (e.g., `EMP0001`). MongoDB internal `_id` is kept internal and not exposed to API consumers.

2. **Absence of ID in `attendance_logs`**:
   Per the domain contract, attendance records are uniquely keyed by the compound pair `(emp_code, date)`. No separate synthetic surrogate key is required.

3. **Absence of Punch Endpoint for `ABSENT` / `LEAVE`**:
   The `/attendance/punch-in` route represents physical attendance events (`PRESENT`, `WFH`, `ON_DUTY`). Non-working statuses (`ABSENT`, `LEAVE`) are managed through regularization (`PATCH`) or automated shift seeding.
