# REVIEW.md — starter defects found and fixed (all in `app/main.py`)

| # | Where | What is wrong | How noticed (test/symptom) | How fixed |
|---|---|---|---|---|
| 1 | imports/config | `load_dotenv()` overrides real env; naive client datetimes | OS `MONGO_DB` ignored; naive vs aware mismatch | `override=False`, `tz_aware=True tzinfo=utc`, 2s timeout |
| 2 | startup | No indexes created | Races + COLLSCAN at 100k | `ensure_indexes()` incl. 2 unique + 4 supporting |
| 3 | `GET /health` | Always 200, never pings | Dead Mongo still 200 | ping → 200 else 503 JSON |
| 4 | `compute_late_minutes` | `int(sec/60) if >10` naive; `09:40:01`→0, negatives wrong | Boundary probes | IST-aware, micros truncated, `<=600s→0` else floor |
| 5 | `compute_overtime` | Naive, no 30-min threshold, no overnight next-day end | `29→29`, night shift wrong | IST end (+1d overnight), floor, `>=30` |
| 6 | `compute_work_hours` | `round(float)` banker's/float error | `1.005h→1.0` | Decimal half-up 2-dec |
| 7 | `POST /employees` | check-then-insert race; naive `created_at`; leaks `_id` shape; no validation | Parallel 201+201; wrong ms | Direct insert + `DuplicateKeyError→409`, UTC truncated, allow-list + regex/date/shift validators |
| 8 | `GET /employees` | `skip=page*size`; `total` unfiltered; no sort/validation; BSON dates | Page 1 skips 20 | `(page-1)*size`, filtered count, `emp_code` sort, Query bounds, ms serialize |
| 9 | `punch-in` | `KeyError→500` unknown emp; any status; naive/local date; race; `id` leak | Unknown emp 500; ABSENT accepted | 404, enum PRESENT/WFH/ON_DUTY, strict ms, IST/overnight date, insert+409, exact 10 fields |
| 10 | `GET /attendance` | Python load+sort, wrong sort/total, `id` leak, no validation | 100k OOM; page wrong | DB find/count/sort `(date -1, emp 1)`, 422 rules, ms serialize |
| 11 | punch-out | Missing entirely | Contract 404/200 absent | Added: latest `punch_in<=ts`, 422 bounds, atomic conditional update (409 loser) |
| 12 | PATCH | Missing entirely | No audit trail | Added: editable-only + derived recompute + `$set+$push history` (atomic per doc) |
| 13 | analytics | Missing | No summaries | Added: DB aggregations (monthly/summary/board/trend with `$densify`) |
| 14 | explain | Missing | No proof | Added: real `executionStats` explain for 5 mains; unknown→422, DB fail→502 |
| 15 | analytics R7 weekend | `is_weekday = $dayOfWeek < 7` = Sun–Fri, so **Sunday counted as present** (MongoDB `$dayOfWeek`: 1=Sun…7=Sat) | Latent — Task 5 seed used a Monday; caught in Task 7 audit | `$in [2,3,4,5,6]` = Mon–Fri, matches Python `weekday()<5`. Verified Sunday excluded from `present_days` but still in late/OT totals |
| 16 | `RegularizeIn` bounds | `reason min_length=1`, no `regularized_by` bounds; contract: reason `min 5 / max 200`, by `min 1 / max 50` | Contract line-by-line vs `RegularizeRequest` | `Field(min_length=5, max_length=200)`, `Field(min_length=1, max_length=50)` |
| 17 | PATCH R1 | Did not enforce `punch_in` staying on the record's attendance `date` | Contract: "`punch_in` must stay on the record's attendance date (R1)" | Reuses `ist_date_str(...) != date → 422` (overnight-safe); verified fires independently of the 24h check |

Not defects: contract `{"detail"}` error envelope; `Decimal` half-up helpers; `extra="ignore"`
for derived/read-only fields (contract says ignore, not reject).

## Coverage actually executed

- `py_compile` pass; `TestClient` (venv-only `httpx`) vs mocks/disposable timestamped DBs
  (created + dropped; live `attendance_db` stayed `0/0`; `sample_seed.py` never run).
- Ran: validation matrices, 409/404/422 paths, pagination/sort Total, R1–R5 boundaries,
  overnight shifts, tie `1,1` + `limit=1`, zero-log headcount, weekend/null rates, half-day 0.5,
  explain IXSCAN/no-COLLSCAN on small isolated data (100k grader set not available locally).
- Task 7 audit re-run: `21/21` HTTP checks on an isolated disposable DB — Sunday-excluded
  present_days (monthly+summary) with Sunday still in late/OT, Regularize bounds 422, valid
  PATCH 200 + history, PATCH off-date 422 (independent of 24h check), health/list/pagination
  regression, no `_id` leak. `audit7_test_db` created and dropped; `attendance_db` untouched.
- Not verified: true parallel-race timing, 100k latency, Atlas vs local plan parity, perf
  of per-department summary fan-out at 100x scale.
- **Python 3.11+/3.12 verified:** final run on Python 3.12.3 — fresh `.venv`, deps installed,
  `py_compile` clean, full HTTP regression suite **49/49** on an isolated disposable DB
  (`attendance_db` stayed `0/0`, `sample_seed.py` never run).

