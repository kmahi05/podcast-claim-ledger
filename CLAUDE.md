# CLAUDE.md — Podcast Claim Ledger

Podcast Claim Ledger is an editorial tool for English-language podcast teams. It transcribes each episode
with local faster-whisper (English-only models), extracts factual claims per 45 s transcript window with
Gemini, links each new claim to the 8 nearest earlier claims from other episodes (`supports` /
`contradicts` / `updates` / `duplicates`), and produces a pre-publish report. Every claim, citation and
answer links to a playable moment (`/w/{ws}/episodes/{id}?t=<seconds>`). **`PRD.md` is the source of
truth.** Read the relevant section before you build anything: §2 architecture, §3.1 DDL, §3.2 Chroma,
§3.3 structured outputs, §4 API, §5 roadmap, §6 guardrails, failure modes and env vars. If this file and
the PRD disagree, follow the PRD and fix this file. Work items live in `TASKS.md`.

## Stack and layout

- **web/**: Vite + React 19 + TypeScript, Tailwind v4 (`@tailwindcss/vite`), `react-router` v7, TanStack
  Query. Later: `wavesurfer.js`, `@tanstack/react-virtual`. Dev server on **3002**, proxies `/api` → 8002.
- **api/**: Python 3.12 (uv), FastAPI, Pydantic v2, SQLAlchemy 2.0 async + `asyncpg` (raw SQL via
  `text()`, no ORM models), PyJWT, argon2-cffi, slowapi, `chromadb` PersistentClient,
  `langchain-google-genai`. Added later by the task that first imports them: `faster-whisper`,
  `rapidfuzz`, `webvtt-py`, `python-multipart`; optional extra `diarize` adds `pyannote.audio`.
- **db/**: PostgreSQL 16, plain SQL migrations. No Supabase, no Docker.

```
db/migrations/0001_init.sql   full §3.1 schema: auth tables, tenancy, content, RLS, create_workspace(), grants
scripts/db_setup.py           roles pcl_owner / pcl_app / pcl_worker + databases podcast_ledger(_test)
scripts/migrate.py            applies db/migrations in name order, tracked in schema_migrations
api/app/
  main.py          create_app(), lifespan (engines, Chroma + embedding-model check), CORS, /health
  config.py        pydantic-settings Settings (reads ../.env); JWT iss/aud constants
  errors.py        AppError + handlers -> {"error":{"code","message"}}
  db.py            init_engines(), user_session(user_id), worker_session()
  llm.py           chat_model(strong=False), embeddings(), use_fakes() for tests
  vectorstore.py   open_client(), ensure_collections(), tenant_where() — the ONLY Chroma filter builder
  storage.py       local audio storage: path validation, save_upload(), signed_url(), /media route
  ratelimit.py     slowapi limiter
  auth/            passwords.py (argon2), tokens.py (JWT + refresh rows), deps.py (get_current_user), router.py
  workspaces/      router.py: /me, /workspaces, /workspaces/{id}/members (invite flow)
  (later)          shows/, episodes/, jobs/, claims/, search/, ask/, admin/ routers;
                   pipeline/ runner, audio, transcribe, diarize, vtt, windows, index, claims, link, report;
                   services/ search (RRF), qa, reindex, backlog; prompts.py
api/tests/         conftest.py (real Postgres test DB), test_auth, test_workspaces, test_rls_tables, test_media, test_unit
web/src/
  main.tsx                     providers + routes
  lib/api.ts                   request(), ApiError, refreshSession() (single-flight), API types
  lib/auth.tsx                 AuthProvider: in-memory access token, api() with refresh-once-on-401
  lib/ui.ts                    shared Tailwind classes, errorMessage()
  pages/                       Login, Register, AppLayout (header + logout), Home (workspaces), AuthForm
```

## Commands

Run from the repo root unless a `cd` is shown. Every line works in Git Bash and PowerShell (use `;`, never `&&`).

```
# one-time setup (PostgreSQL 16 must be running on localhost:5432, any install method)
winget install Gyan.FFmpeg          # needed from the transcribe task on; check: ffmpeg -version
winget install astral-sh.uv         # or: pip install uv
cp .env.example .env                # PowerShell: Copy-Item .env.example .env
cd api; uv sync                     # add --extra diarize only if you need pyannote (later task)
cd web; npm install

# database
cd api; uv run python ../scripts/db_setup.py     # idempotent: roles + podcast_ledger + podcast_ledger_test
cd api; uv run python ../scripts/migrate.py      # add --database-url <url> to target another DB
pg_isready -h localhost -p 5432                  # psql/pg_isready live in your PostgreSQL bin folder

# run
cd api; uv run uvicorn app.main:app --reload --port 8002
cd web; npm run dev                              # http://localhost:3002

# test / lint
cd api; uv run pytest -q                         # drops + recreates podcast_ledger_test, runs migrations
cd api; uv run pytest tests/test_auth.py::test_refresh_rotation -q   # single test
cd api; uv run ruff check . ../scripts; uv run ruff format --check . ../scripts
cd web; npm run lint; npm run typecheck; npm run build
```

Later markers: `-m slow` = real faster-whisper on fixtures, `-m e2e` = real Gemini calls (needs
`GOOGLE_API_KEY`). Add them to pytest `addopts` exclusion when the first such test lands.
CPU mode uses `small.en` (about 1–2x real time). `medium.en` and `large-v3-turbo` (GPU, CUDA 12 + cuDNN 9)
are options; always call `transcribe(..., language="en")`.
Local Chroma data lives in `CHROMA_PATH`; delete that folder and call `POST /api/v1/admin/reindex` to rebuild.

## Environment variables (one root `.env`; full table with defaults in PRD §6)

`PG_SUPERUSER_URL` (scripts/tests only), `MIGRATE_DATABASE_URL` (pcl_owner), `APP_DATABASE_URL` (pcl_app),
`WORKER_DATABASE_URL` (pcl_worker), `JWT_SECRET` (≥ 32 bytes), `ACCESS_TOKEN_TTL_SECONDS` (900),
`REFRESH_TOKEN_TTL_SECONDS` (1209600), `COOKIE_SECURE` (false in dev), `WEB_ORIGIN` (http://localhost:3002),
`GOOGLE_API_KEY`, `GEMINI_MODEL` (gemini-2.5-flash), `GEMINI_MODEL_STRONG` (gemini-2.5-pro),
`GEMINI_EMBEDDING_MODEL` (models/gemini-embedding-001), `LLM_CONCURRENCY` (4), `CHROMA_PATH`
(./data/chroma), `STORAGE_DIR` (./data/storage), `FILE_URL_SECRET` (≥ 32 bytes), `WHISPER_MODEL`
(small.en; must be `*.en` or `large-v3-turbo`), `WHISPER_DEVICE` (cpu), `WHISPER_COMPUTE_TYPE` (int8),
`HF_TOKEN` (only for the optional diarize extra), `AUDIO_RETENTION_DAYS` (0).
Web: `web/.env` → `VITE_API_PROXY_TARGET` (http://localhost:8002).
When you add a setting, add it to `app/config.py`, `.env.example`, PRD §6 and this list in the same change.

## Architecture rules (never break these)

**Database, tenancy, auth**
1. Three roles. `pcl_owner` owns tables and runs migrations only. `pcl_app` (no BYPASSRLS) serves user
   requests: always `async with db.user_session(user.id) as conn`, which sets `app.user_id` for RLS.
   `pcl_worker` (BYPASSRLS) is for pipeline stages, auth routes, admin tasks and lookups the app role
   cannot do (e.g. other users' emails). **Every worker query filters by `workspace_id`/`episode_id`
   explicitly**, and a worker route that acts for a user checks membership first.
2. `public.create_workspace(name)` is the only way to create a workspace (it adds the caller as owner).
   `pcl_app` has no insert grant on `workspaces`.
3. RLS is enabled on every table: read for any member (`ws_role(workspace_id) is not null`), write for
   owner/editor; `workspace_members` writes for owners. Policies use `(select app.current_user_id())`.
   Every new table ships in a migration with RLS + policies + grants and a pytest RLS test.
4. Auth: argon2id hashes; HS256 access tokens (15 min, iss `podcast-ledger`, aud `podcast-ledger-api`,
   `typ=access`, algorithms pinned); opaque refresh tokens stored as sha256, rotated on every use;
   reuse of a revoked token revokes all the user's tokens. The SPA keeps the access token in memory; the
   refresh token is an httpOnly cookie (`path=/api/v1/auth`, `SameSite=Strict`). Login is 10/min per IP.
5. Never change an applied migration; add `db/migrations/NNNN_name.sql`.

**Storage**
6. Audio lives on local disk at `STORAGE_DIR/{workspace_id}/{episode_id}.{ext}`; only `app/storage.py`
   builds or resolves paths, and it rejects anything else. Uploads stream in 1 MB chunks with the 500 MB
   cap. Playback uses `storage.signed_url()` (HMAC, 600 s) served by `/api/v1/media/...` with Range
   support. Shortcut: single API server; move to S3-compatible storage before running 2+ instances.

**LLM and vectors**
7. Gemini only (`langchain-google-genai`); get models from `app.llm.chat_model()` / `chat_model(strong=True)`
   / `embeddings()`. No OpenAI or Anthropic packages. Tests never call Gemini: use `llm.use_fakes(...)`
   with `FakeListChatModel` / `DeterministicFakeEmbedding`.
8. Every Chroma `query`/`get`/`delete` uses `where=tenant_where(workspace_id, ...)` and passes our own
   embeddings (`query_embeddings=`, `embeddings=`); collections have no embedding function. Collection
   access stays in `app/vectorstore.py` helpers or pipeline modules that import them.
9. Postgres is the truth and Chroma is a rebuildable index. `POST /admin/reindex` must rebuild both
   collections from Postgres alone. Startup refuses a collection whose `embedding_model` metadata differs
   from `GEMINI_EMBEDDING_MODEL`.

**Pipeline and guardrails**
10. The LLM never decides a timestamp. Parse its `[mm:ss]` marker, then snap to the last segment in that
    window with `start_ms <= t`; the claim gets that segment's `start_ms` and `end_ms`.
11. The LLM never decides that a quote exists. Drop any claim whose `verbatim` fails
    `rapidfuzz.fuzz.partial_ratio >= 85` against the window's transcript text.
12. The LLM never compares numbers. If both claims have `quantity` and `unit` and the same `subject_lc`, a
    relative difference > 1% forces `contradicts` and appends "numeric mismatch" to the rationale.
13. The LLM never confirms anything. New links are `status='suggested'`; only a human PATCH sets
    `confirmed`/`rejected`. Counts and filters include every non-rejected link; UI and report say
    "possible contradiction" until confirmed.
14. The LLM never invents link targets or citations. Drop a `Link.other_id` not in the candidate list; drop a
    `[[window_id]]` not in the retrieved set.
15. Keep only links with `relation != 'unrelated'` and `confidence >= 0.6`. Drop candidates with cosine
    distance > 0.45 before the LLM; if none remain, make no LLM call. `/claims/check` reuses this chain.
16. Linking only looks backwards: other episodes, `published_ts <= this episode's published_ts`
    (`published_ts` falls back to `created_at`).
17. `/ask` returns `insufficient_context: true` when no window has distance <= 0.5 and FTS has no hit.
    Hybrid = Chroma top-12 + `websearch_to_tsquery` top-8, RRF k=60, top 8. `/search/moments` modes:
    `hybrid | semantic | keyword`.
18. Six stages in order: transcribe → diarize (`skipped` unless requested) → index → extract_claims →
    link_claims → report. A stage starts only if the previous one `succeeded`/`skipped`; insert the next
    stage's job row only when the previous finishes (`pipeline_jobs_one_active`). `/process` returns the
    first job id. Re-running a stage deletes only that stage's outputs for that episode.
19. CPU-bound work never runs on the event loop: Whisper in the single `ProcessPoolExecutor(max_workers=1)`;
    ffmpeg via `asyncio.create_subprocess_exec`; big rapidfuzz work via `asyncio.to_thread`.
20. Before `extract_claims` deletes claims, copy human-decided links to `preserved_claim_links`; re-attach
    when a claim with matching verbatim (partial_ratio >= 85) and `start_ms` within ±2 s comes back.
21. Transcript text is untrusted data: wrap it in `<transcript>` delimiters, give chains no tools, always
    validate through `with_structured_output`.
22. Skip `low_confidence` windows (mean `avg_logprob` < -1.0) in claim extraction; grey them out in the UI.
23. On startup, mark `running` jobs older than 2x the episode duration as `failed`.
24. LLM calls: semaphore of `LLM_CONCURRENCY`, exponential backoff with jitter, 3 tries. A stage succeeds if
    >= 95% of its windows/claims succeed; failures are listed in `pipeline_jobs.error`.

## Coding conventions

- Every endpoint is `async def`. Routes are thin; SQL and business logic go in `services/` or `pipeline/`
  once those exist. Raw SQL with `sqlalchemy.text()` and named params; never f-string user input into SQL.
- Pydantic v2 only. Validation at the boundary (request models with `Field`/`StringConstraints`).
- Errors: raise `AppError(status, code, message)` → `{"error":{"code":"snake_case","message":"…"}}`.
  Codes: `unauthorized`, `invalid_credentials`, `forbidden`, `not_found`, `conflict`, `email_taken`,
  `already_member`, `invalid_invite`, `validation_error`, `file_too_large`, `rate_limited`,
  `upstream_error`, `internal_error`.
- Pagination: opaque base64 `cursor` of `(created_at, id)` or `(start_ms, id)`; `{"items":[…],"next_cursor"}`.
- Times are integer ms in the API and DB. Only the UI converts to `mm:ss`; the `?t=` URL param is seconds.
- Naming: snake_case Python/JSON; Chroma window id `f"{episode_id}:w{index:05d}"`; Chroma claim id =
  `claims.id`; migrations `NNNN_<verb>_<thing>.sql`; React components PascalCase, hooks `useX`.
- Python: ruff (line length 100, E,F,I,B,UP,ASYNC), type hints on public functions, `logging` not `print`
  (scripts may print).
- TypeScript: `strict: true`, no `any`. Server state in TanStack Query. All calls go through
  `useAuth().api()` (or `request()` for unauthenticated auth calls).

## Testing rules

- pytest with a real PostgreSQL test DB (`podcast_ledger_test`): `conftest.py` drops/recreates it and runs
  `scripts/migrate.py` once per session. If Postgres is unreachable, DB tests skip with
  "PostgreSQL not reachable at <url>". These pytest RLS tests replace pgTAP.
- Every new table: a test that a member of workspace B sees 0 of A's rows on the app role
  (`set_config('app.user_id', …)`), that a viewer cannot write, and that the worker role sees both.
- Must have unit tests (later tasks): `build_windows` edges; `parse_vtt`; `parse_mmss` + `snap_to_segment`;
  `verbatim_ok`; `numeric_override`; `filter_links`; `rrf_merge`; `check_citations`; `merge_speakers`;
  job state machine; stale-job recovery.
- Gemini is faked (`llm.use_fakes`). Real calls only under `-m e2e`; real Whisper only under `-m slow`.
- Exit check (before Phase 3 UI work): `uv run pytest -m e2e tests/e2e/test_fixture_pipeline.py` finds the
  planted 1932-vs-1937 contradiction within ±3 s.
- Before marking a task done: `uv run ruff check . ../scripts`, `uv run ruff format --check . ../scripts`,
  `uv run pytest -q`, and for web `npm run lint`, `npm run typecheck`, `npm run build`.

## Do not

- Do not use Supabase, Docker, OpenAI, Anthropic or BGE anything. Do not add faster-whisper/ffmpeg code
  until the transcribe task.
- Do not run user requests on the worker engine without an explicit membership check and workspace filter.
- Do not build Chroma `where` dicts by hand, or call Chroma with `query_texts`.
- Do not load `WhisperModel` in the API process or at import. Load it once in the pool worker's
  initializer (Windows uses `spawn`, so the worker function must be top-level). Always `language="en"`;
  no language detection, no multilingual model setting.
- Do not create all stage job rows up front; that breaks `pipeline_jobs_one_active`.
- Do not trust LLM `speaker` text over segment data; fall back to the snapped segment's speaker.
- Do not keep the 16 kHz temp WAV after transcription; delete it in a `finally`.
- Do not show a `suggested` contradiction as fact anywhere.
- Do not put tokens in `localStorage`; do not return secrets or password hashes from any endpoint.
- Do not use bash-only syntax (`&&`, `$(…)`, `export`) in documented commands.
- Do not commit fixture audio without a licence/source line in `api/tests/fixtures/README.md`.

## Workflow

- Work through `TASKS.md` from top to bottom. Each task's `Depends on:` must be ticked before you start it.
- When a task's `Done when:` check passes, change `- [ ]` to `- [x]` in the same change.
- Keep tasks in dependency order. Add missing work as a new task with the next free ID in the right phase;
  never renumber.
- When a convention, command or env var changes, update this file in the same change.
- Commit one task per commit: `T2.12: build_windows with edge-case tests`.
