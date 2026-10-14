# Employee Attendance & Analytics API

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or use a local .env
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$setWindowFields`, correlated `$lookup`). Indexes are created at startup.
All code is in `app/main.py`. `python -m pytest tests -q` runs the business-rule unit tests (no database needed).

## Notes for the reviewer
- **Trend gap filling** uses `$range` + `$map` inside the pipeline (one row per calendar day, generated in the database) instead of `$densify`, so it also works when a department has no records at all in the range.
- **Trend moving average** averages the *unrounded* daily rates; only reported values are rounded (half-up).
- **PATCH conflicts** return 409 (optimistic check on history length and the record's current status and punch times); the client retries.
- **PATCH** with `status` omitted on an `ABSENT`/`LEAVE` record plus punch times is a 422 (a presence status is required).
- `GET /attendance` uses `skip`/`limit` paging and `count_documents()` so `total` exactly reflects the applied filters (R10).

## Not verified
This was written without access to a MongoDB instance. The pure rules (R1-R8) are unit-tested; the endpoints and pipelines still need a run against a real database, including the awkward cases (ties, no-log employees, weekend records, mid-month joiner, overnight shift, a missing weekday).
