# Podcast Claim Ledger

An editorial tool for English-language podcast teams: it transcribes episodes, extracts each factual claim,
links claims across the back catalog (`supports` / `contradicts` / `updates` / `duplicates`) and flags
possible contradictions with a link that plays the audio at the exact second.

The full design is in [PRD.md](PRD.md); work items are in [TASKS.md](TASKS.md); conventions for
contributors (and coding agents) are in [CLAUDE.md](CLAUDE.md).

## Status

Boilerplate: database schema with row-level security, email/password auth (JWT + rotating refresh
cookie), workspaces with invites, Chroma setup, signed audio URLs, and a React app where you can sign
up, sign in and create workspaces. The transcription and claim pipeline are next (see TASKS.md).

## Prerequisites

- PostgreSQL 16 on `localhost:5432` (any install method), superuser `postgres` / `postgres` for dev.
- [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`). uv downloads Python 3.12 itself.
- Node.js 20+ and npm.
- `ffmpeg` on PATH, needed once transcription lands: `winget install Gyan.FFmpeg`, then open a new
  terminal and check `ffmpeg -version`.
- A Gemini API key (`GOOGLE_API_KEY`) for the LLM stages. Not needed for the boilerplate or the tests.

## Setup (in order)

Commands work in PowerShell and Git Bash.

```
cp .env.example .env                     # PowerShell: Copy-Item .env.example .env
cd api
uv sync
uv run python ../scripts/db_setup.py     # roles pcl_owner/pcl_app/pcl_worker + databases podcast_ledger(_test)
uv run python ../scripts/migrate.py      # applies db/migrations/*.sql as pcl_owner
cd ../web
npm install
```

The scripts read `PG_SUPERUSER_URL`, `MIGRATE_DATABASE_URL`, `APP_DATABASE_URL` and
`WORKER_DATABASE_URL` from the environment and fall back to the dev defaults in `.env.example`.

## Run

```
cd api; uv run uvicorn app.main:app --reload --port 8002
cd web; npm run dev                      # http://localhost:3002 (proxies /api to :8002)
```

## Test and lint

```
cd api; uv run pytest -q                 # recreates podcast_ledger_test; DB tests skip if Postgres is down
cd api; uv run ruff check . ../scripts; uv run ruff format --check . ../scripts
cd web; npm run lint; npm run typecheck; npm run build
```

## Layout

- `api/` FastAPI app (`app/`), tests (`tests/`).
- `web/` Vite + React + TypeScript + Tailwind v4.
- `db/migrations/` plain SQL migrations.
- `scripts/` `db_setup.py`, `migrate.py`.

## License

MIT, see [LICENSE](LICENSE).
