# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
   - `employees`: unique `emp_code` enforces business identity; `(department, emp_code)` satisfies filtered sorting; `(department, joined_on)` and `(joined_on)` optimize headcount range filters.
   - `attendance_logs`: unique `(emp_code, date)` guarantees at most one record daily; `(emp_code, punch_in: -1)` speeds open record lookups; `(date: -1, emp_code: 1)` covers pagination; `(date, late_minutes, emp_code)` backs late analytics.
   - *Rejected*: heavy covered indexes containing all projection fields, protecting write throughput and RAM on high-frequency punch paths.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
   - Both concurrent requests compute the IST date and call `insert_one()`. MongoDB's write lock and the unique compound index on `(emp_code, date)` ensure exactly one succeeds (returning 201). The duplicate attempt raises `pymongo.errors.DuplicateKeyError`, which the API catches and maps to HTTP 409 Conflict.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
   - The pipeline applies `$setWindowFields` with `$rank` (Standard Competition Ranking: 1, 2, 2, 4), filtering `rank <= limit` afterward. If two employees tie at the cutoff rank, both are returned (e.g. 11 rows) rather than arbitrarily omitting one. Tied rows sort by `emp_code` ascending.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
   - The pipeline originates on `employees` (`joined_on <= month_end`) and `$lookup`s into `attendance_logs`. Employees with zero monthly logs remain intact, contributing 1 to headcount while their attendance metrics resolve to zero.

5. **One thing you would change** if this had to serve 100x the data.
   - I would introduce asynchronous end-of-day materialized rollups into a daily summary collection, allowing monthly analytics to scan 30 daily summaries instead of millions of raw punch records.
