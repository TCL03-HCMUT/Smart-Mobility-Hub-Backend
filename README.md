# Smart Mobility Backend

FastAPI modular-monolith backend for the React frontend. The package mirrors the
database domains while keeping HTTP, business, and persistence concerns separate.

## Layout

- `app/api/routes` — REST endpoints and transport concerns.
- `app/schemas` — Pydantic request/response contracts.
- `app/services` — use-case rules and module workflows; services depend on repositories only through `UnitOfWork`.
- `app/repositories` — SQLAlchemy data-access helpers.
- `app/models` — ORM mapping for the supplied SQLite schema.
- `app/core` — configuration, security, utilities, and exceptions.
- `app/database.py` — engine, sessions, and SQLite foreign-key setup.
- `tests` — API and service tests.

## Run

Use Python 3.11+. The demo uses a local SQLite file. PostgreSQL is part of the full system design.

1. Create and activate a virtual environment from the project root:

   **macOS / Linux (`bash` / `zsh`):**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   **Windows (PowerShell):**

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   > On Windows, if `python` is not on `PATH`, use `py -3` instead. If PowerShell blocks activation scripts, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use Command Prompt (`.venv\Scripts\activate.bat`) or Git Bash (`source .venv/Scripts/activate`).

2. Install dependencies, seed the demo database, and start the server (same on all platforms once `.venv` is activated):

   ```bash
   python -m pip install -e ".[test]"
   python -m app.demo seed
   python -m uvicorn app.main:app --reload
   ```

API documentation is available at `http://localhost:8000/docs`.

Demo accounts are `anh@demo.example` and `bao@demo.example` (students), and `nam@demo.example` (operator). All use the password `gogreen-demo`.

For a repeatable demo, copy `.env.example` to `.env` (`cp .env.example .env` on macOS/Linux/PowerShell/Git Bash, or `copy .env.example .env` in Windows Command Prompt) and set `DEMO_CLOCK_UTC` before seeding and starting the server.

Run `python -m pytest -q` inside the activated virtual environment for tests. See the [OpenAPI contract](docs/openapi.json).
