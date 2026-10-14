# REVIEW.md

Defects found in the starter `app/main.py`. "Fixed" refers to the new `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Created without `tz_aware=True`, so every datetime read is naive; naive and aware values are then mixed (and `datetime.now()` / `fromtimestamp()` use the *server's* local zone). | `TypeError: can't subtract offset-naive and offset-aware datetimes` in `compute_overtime`; wrong dates when the server is not in UTC. | `tz_aware=True, tzinfo=UTC`; all instants are aware UTC; `to_ms`/`from_ms` convert at the boundary. |
| 2 | `compute_late_minutes` | `replace(hour=..)` keeps the **UTC** date and time-of-day; shift start is IST. | Employee punching at 09:35 IST (04:05 UTC) is "late by hours" / negative. | Shift start built as an IST datetime for the attendance date. |
| 3 | `compute_late_minutes` | `int(seconds/60) > 10` turns "strictly more than 10:00" into "11 whole minutes". Seconds are not truncated. | Shift 09:30, punch 09:40:01 should be 10, starter gives 0. | Compare whole seconds against 600; `late = floor(secs/60)`. |
| 4 | `compute_work_hours` | Float `round()` (banker's / binary), not half-up; no truncation to whole seconds. | 18 s of work -> `0.0` instead of `0.01`; 2.675-type values round down. | `Decimal(seconds)/3600`, `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum; shift end always on `date_str` (wrong for overnight shifts); naive vs aware subtraction. | Punch-out 5 min after shift end returns 5; overnight shift returns ~16 hours; 500 on naive data. | Threshold of 30; end is on the next day for overnight shifts; aware datetimes. |
| 6 | `EmployeeIn` | No validation: any `emp_code`, email, `shift_*`, `joined_on`; `shift_start == shift_end` allowed. | `POST` with `emp_code="x"`, `shift_start="25:99"` returns 201. | Patterns/lengths from the contract, real date check, start != end -> 422. |
| 7 | `create_employee` | Check-then-insert race, no unique index; `datetime.now()` naive local; `created_at` returned as a datetime, not epoch ms. | Parallel creates give two 201s. | Unique index + `DuplicateKeyError` -> 409; UTC `created_at`, returned as ms. |
| 8 | `list_employees` | `skip = page*page_size` (page 1 skips the first page); `total` ignores the filter; no sort; no bounds on `page`/`page_size`. | `GET /employees?page=1` misses the first 20; `?page=0` / `page_size=-1` -> 500. | `(page-1)*size`, filtered count, sort by `emp_code`, `Query(ge, le)` bounds. |
| 9 | `punch_in` | Unknown employee -> `emp` is `None` -> `TypeError` (500). | `POST` with an unknown `emp_code`. | 404. |
| 10 | `punch_in` | Date from server-local naive time, not IST; no overnight rule (R1); seconds not truncated; `if body.punched_at` treats 0 as missing. | Punch at 00:30 IST gets the previous UTC date; overnight punch-in at 03:00 lands on the wrong day. | `attendance_date()` per R1; truncate to seconds; `is not None`. |
| 11 | `punch_in` / `PunchInIn` | Check-then-insert race; no unique index. `status` is any string (ABSENT, "foo" accepted). `punched_at` accepts seconds, floats-as-ints, numeric strings. | Two parallel requests both get 201; `{"status":"ABSENT"}` returns 201; `punched_at=1783312500` is accepted. | Unique `(emp_code,date)` index -> 409; `Literal` presence status; `StrictInt` within `[1e11, 4.1e12]`. |
| 12 | `punch_in` response | Returns `id` (ObjectId) and datetimes as ISO strings, not epoch ms. | Response has `"id"` and `"punch_in": "2026-..."`. | `record_out()`: no `_id`, epoch ms, contract fields. |
| 13 | `list_attendance` | Loads the whole collection into memory, sorts and slices in Python; no indexes. | ~100k docs: seconds per request and high memory. | Server-side sort/skip/limit on indexes; count via `count_documents` / `estimated_document_count`. |
| 14 | `list_attendance` | Sort only by `date` (unstable ties); no validation of `page`, `page_size`, dates, `status`; `date_from > date_to` not rejected; leaks `_id` as `id`; legacy docs missing `history`/`half_day` come back incomplete. | `page_size=100000`; `date_from=garbage`; ordering changes between pages. | Sort `date desc, emp_code asc`; validation -> 422; `record_out()` defaults missing fields. |
| 15 | `/health` | Always `ok`, never touches MongoDB. | Stop Mongo: still 200, runner starts testing too early. | `ping`, 503 on failure. |
| 16 | Startup | No index creation at all; client has the 30 s default server-selection timeout. | Everything is a collection scan; a down DB hangs requests for 30 s. | Idempotent `ensure_indexes()` in the lifespan; 5 s timeout; `PyMongoError` -> 503. |
| 17 | `create_employee` | `insert_one` mutates `doc` and the response reuses it; only `_id` is popped by hand. | Fragile: any added internal field leaks. | Explicit `employee_out()` serializer. |

**Looked at and judged fine**
- `load_dotenv()` without `override=True`: real environment variables already win, as required.
- Sync (`def`) endpoints with a shared `MongoClient`: FastAPI runs them in a threadpool and PyMongo's client is thread-safe, so parallel requests are fine.
- Default `MONGO_URI` / `MONGO_DB` fallbacks: only used when the variables are absent; nothing is hard-coded for the grader.
