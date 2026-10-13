# HROne — Employee Attendance & Analytics API

FastAPI + MongoDB service implementing `openapi.yaml` (contract v2.0): employees, punch-in/out,
attendance list, regularization with audit history, four analytics endpoints, and an explain endpoint.

## Setup

- Python **3.11+ required** by the assignment. **Verified on Python 3.12.3** (fresh `.venv`, deps installed,
  `py_compile` clean, full HTTP regression suite 49/49). No 3.11-only syntax is used.
- MongoDB **6.0+** (tested on local 8.0.3; grader uses 7.0). Operators used
  (`$match/$group/$sort/$addFields/$dayOfWeek/$dateFromString/$dateToString/$densify/$ifNull/$cond/$in`)
  all exist in 7.0.
- Install: `python -m venv .venv`, `./.venv/Scripts/python.exe -m pip install -r requirements.txt`
  (`requirements.txt` unchanged: fastapi, uvicorn, pymongo, pydantic, python-dotenv).
  `httpx` was installed **venv-only** for `TestClient`; it is not a runtime dependency.

## Environment

- `MONGO_URI` (default `mongodb://localhost:27017`), `MONGO_DB` (default `attendance_db`).
- OS environment wins over `.env` (`load_dotenv(override=False)`); nothing hard-coded.
- Do **not** commit `.env`, secrets, `.venv`, or dumps. `sample_seed.py` was never run during this work.

## Run

From the repository root: `uvicorn app.main:app --port 8000` (ready when `GET /health` is 200;
Mongo ping failure returns 503; 2s server-selection timeout keeps startup under 20s).

## Endpoints

- `GET /health`, `POST+GET /employees`, `POST /attendance/punch-in|punch-out`,
  `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`,
  `GET /analytics/employees/{code}/monthly`, `GET /analytics/departments/summary`,
  `GET /analytics/leaderboard/late`, `GET /analytics/departments/{dept}/trend`,
  `GET /admin/explain/{endpoint}` (attendance_list, employee_monthly, department_summary,
  late_leaderboard, department_trend; real `executionStats` explain, no fabricated plans).

## Tests

- `python -m py_compile app/main.py`; HTTP checks via `TestClient` against mocks or
  timestamped disposable databases (created, then dropped); live `attendance_db` verified `0/0`
  after runs. No test suite is committed; see REVIEW.md for exact probes and gaps.

