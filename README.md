# Smart E-Mobility Hub backend

API for the gogreen course project. It manages shared rentals, parking, charging, incidents, vehicle dispatch, and What-if simulation.

**Stack:** Python, FastAPI, Pydantic, SQLAlchemy, SQLite, and Uvicorn. The demo runs on SQLite. PostgreSQL is part of the full system design.

## Folder structure

| Folder | Contents |
|---|---|
| `app/api/routes/`, `app/schemas/` | HTTP endpoints and request/response models |
| `app/services/` | Business rules and simulation |
| `app/models/`, `app/repositories/` | Database models and queries |
| `app/core/`, `app/demo.py` | Configuration, authentication, and demo seed |
| `docs/`, `tests/` | OpenAPI contract and backend tests |

## Run locally

Use Python 3.11+. From this repo, run:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
cp .env.example .env
python -m app.demo seed
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`seed` creates the configured SQLite schema and adds the demo data. To keep seed battery readings fresh for a repeatable demo, set `DEMO_CLOCK_UTC` in `.env` before running seed and starting the server. See [`.env.example`](.env.example) for an example.

Open [Swagger UI](http://127.0.0.1:8000/docs) or start the companion `se-frontend` app. Demo accounts are `anh@demo.example` and `bao@demo.example` (students), and `nam@demo.example` (operator). All use the password `gogreen-demo`.

Run `python -m pytest -q` for tests. The [OpenAPI contract](docs/openapi.json) is available in this repo.
