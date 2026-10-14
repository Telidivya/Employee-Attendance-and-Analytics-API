# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (one record per day, per-employee queries, monthly summary); `(date desc, emp_code)` (list sort, month scans); `(status, date desc, emp_code)` (status filter); `(emp_code, punch_in desc)` (find the record to punch out); a partial `late_minutes > 0` index (leaderboard). `employees`: unique `emp_code`, `(department, joined_on)`, `(department, emp_code)`, `joined_on`. Rejected: a standalone `emp_code` index (redundant prefix) and copying `department` onto every log.
2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique index lets exactly one succeed; the other raises `DuplicateKeyError`, which I turn into 409. There is no read-then-write.
3. **Ties.** `$rank` runs before the limit, then I keep `rank <= limit`. Everyone tied at the cutoff rank is returned, so the response can exceed `limit`; ties are ordered by `emp_code`.
4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`) and attaches logs with `$lookup`. `$unwind` keeps empty arrays, so employees with zero logs still count.
5. **At 100x.** Replace `skip` paging with keyset paging, and keep pre-aggregated daily rollups per department instead of scanning raw logs.
