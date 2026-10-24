# Podcast Claim Ledger - Product Requirements Document

## 1. Executive Summary & Value Proposition

### Elevator pitch
An editorial tool for podcast and video teams. It transcribes every episode, pulls out each factual claim the hosts make, links claims across the whole back catalog, and flags when a new episode contradicts something said before, with a link that plays the audio at the exact second.

### Target persona and friction
- **Persona:** A producer or research editor at an English-language interview or news podcast with 100–1,500 episodes, often 2–3 shows on one team. Also solo creators who sell sponsorships and need to stand behind what they say.
- **Friction:**
  1. "Did we already cover this? What did the guest say about X in 2024?" Today that means scrubbing hours of audio or keyword-searching bad auto-transcripts.
  2. Hosts contradict themselves across episodes ("we said the startup raised $20M; in episode 212 we said $12M"). Listeners notice before the team does, and corrections hurt credibility.
  3. Show notes and sources are written by hand after each recording.
- **Outcome:** A searchable *claim ledger*. Each claim is a normalized statement with speaker, timestamp and episode, and is linked to related claims as `supports`, `contradicts`, `updates` or `duplicates`. Each new episode gets a pre-publish report: "3 possible contradictions with earlier episodes, 5 numbers need a source".
- **Scope:** podcasts are English only. Non-English audio is out of scope.

### Technical selling points for interviews
1. **Timestamp-anchored RAG.** Every chunk carries `start_ms`/`end_ms`. Every answer and every claim links to a playable moment (`/w/{ws}/episodes/{id}?t=1834`), so a citation can be checked in 5 seconds.
2. **Two-level retrieval.** Transcript windows answer "what was said". A separate claim collection answers "which earlier claims are about the same thing". Contradiction checks compare claims, not raw text.
3. **Candidate generation + structured classification.** Chroma retrieves the 8 nearest earlier claims. The LLM labels each pair with a fixed enum and must quote both sides. This is the standard pattern for scalable NLI-style (natural language inference) matching.
4. **CPU-bound work kept off the event loop.** Whisper transcription runs in a `ProcessPoolExecutor(max_workers=1)`, so the async API stays responsive while a 90-minute episode transcribes.
5. **Tenant isolation in the database.** Every table has PostgreSQL row-level security keyed on the request's user id; the API's request role cannot bypass it. Private audio is served only through short-lived HMAC-signed URLs.

---

## 2. End-to-End System Architecture

### Frontend — React (Vite + TypeScript + Tailwind CSS v4)
A dashboard-style app with no SEO needs, so Vite + React Router is the simpler fit.
- **Routing:** `react-router` v7. **Data:** TanStack Query against FastAPI. All data goes through FastAPI.
- **Dev server:** Vite on port 3002 proxies `/api` to FastAPI on port 8002, so the browser sees one origin.
- **Auth:** email + password against FastAPI (§4 Auth). The access token lives only in memory (React context). The refresh token is an httpOnly cookie set by FastAPI (`path=/api/v1/auth`, `SameSite=Strict`, `Secure` in production). On app load the SPA calls `POST /auth/refresh` to restore the session; on a 401 it refreshes once and retries.
- **Screens:**
  - Shows: a list of shows and episodes with processing status.
  - Upload / Import: create episodes, upload audio, start processing, see stage progress.
  - Episode: transcript with speaker labels, a waveform player (`wavesurfer.js`), and claims highlighted inline.
  - Ledger: a searchable table of claims with filters (show, speaker, type, verification, has-contradiction).
  - Claim: the claim with its linked claims grouped by relation, each with a ▶ play button.
  - Pre-publish report for a newly processed episode.
  - Ask: a question box with timestamped citations.
- **Playback:** the player loads a signed URL (`GET /episodes/{id}/audio-url`, valid 10 min) into `<audio>` and seeks to `t`. A browser `<audio>` tag cannot send an `Authorization` header, so the URL itself carries the proof (`?exp=<unix>&sig=<hex>`).

### Backend API — FastAPI (Python 3.12, Pydantic v2)
- `async def` endpoints. SQLAlchemy 2.0 async with `asyncpg`, raw SQL via `text()`.
- **Auth:** own email/password auth with argon2id hashes and HS256 JWT access tokens (15 min) plus rotating opaque refresh tokens (14 days). See §4.
- **Two DB roles at runtime:**
  - `pcl_app` (no `BYPASSRLS`): every user request runs in `user_session(user_id)`, a transaction that first runs `select set_config('app.user_id', :uid, true)`. RLS policies read it through `app.current_user_id()`.
  - `pcl_worker` (`BYPASSRLS`): background pipeline stages, auth routes (login/register run before a user exists) and admin tasks use `worker_session()`. **Every worker query filters by `workspace_id` and/or `episode_id` explicitly.**
  - `pcl_owner` owns the tables and runs migrations only.
- **Pipeline** (six stages, one `pipeline_jobs` row per stage), started via `BackgroundTasks`:
  1. `transcribe`: `faster-whisper` with an English-only model, `language="en"` forced, `word_timestamps=True`, `vad_filter=True`. Default `small.en` on CPU (`int8`); `medium.en` (CPU or GPU) and `large-v3-turbo` (GPU, `float16`) are options, always with `language="en"`. There is no language detection. Runs in a single-worker process pool. Needs `ffmpeg` on PATH.
  2. `diarize` (optional, off by default): `pyannote/speaker-diarization-3.1` (needs the `diarize` extra and `HF_TOKEN`), merged with segments by max time overlap. When off, the job row is `skipped`; speakers come from the show's `default_speakers` and the user can relabel.
  3. `index`: build windows, embed, upsert to Chroma `transcript_windows`.
  4. `extract_claims`: per window, LLM structured output → `claims` rows → embed into Chroma `claims`.
  5. `link_claims`: per new claim, retrieve the 8 nearest claims from *other, earlier* episodes, LLM pair classification, then `claim_links`.
  6. `report`: SQL aggregate → `episodes.report` jsonb, episode `status='ready'`.
- **Job rows:** a stage's row is inserted when the previous stage finishes (so at most one `queued`/`running` row exists per episode, enforced by `pipeline_jobs_one_active`). `POST /episodes/{id}/process` inserts only the first row and returns its id.
- Stages are resumable: a stage starts only if the previous one `succeeded` (or was `skipped`), and re-running a stage deletes and rewrites only that stage's outputs for the episode.

### Relational DB — PostgreSQL 16
- Plain PostgreSQL 16 (no Supabase). Migrations are plain SQL files in `db/migrations/NNNN_name.sql`, applied by `scripts/migrate.py` and tracked in `schema_migrations`.
- Team workspaces with `owner`, `editor` and `viewer` roles. The only way to create a workspace is the `security definer` function `public.create_workspace(name)`, which also makes the caller its `owner`.
- Postgres holds users, shows, episodes, segments, claims, links, verification status and the job state machine.
- Full-text search: a `tsvector` column on `transcript_segments` for exact phrase matches, which complements semantic search.

### Audio storage — local disk behind FastAPI
- Files live under `STORAGE_DIR` (default `./data/storage`) at `{workspace_id}/{episode_id}.{ext}` (`ext` ∈ `mp3, m4a, wav, mp4`). `episodes.storage_path` stores that relative path. `app/storage.py` rejects any path whose folder is not a canonical UUID or whose file name is not `{uuid}.{ext}`.
- **Upload:** `POST /episodes/{id}/audio` (multipart, field `file`). FastAPI streams the part to disk in 1 MB chunks, enforces the 500 MB cap while copying, whitelists extension and MIME type, writes to a `.part` file and renames it on success.
- **Playback:** `GET /episodes/{id}/audio-url` checks membership and returns a URL to `GET /api/v1/media/{workspace_id}/{episode_id}.{ext}?exp=…&sig=…` (HMAC-SHA256 with `FILE_URL_SECRET`, 10 min). The media route checks the signature and serves the file with HTTP Range support (Starlette `FileResponse`), so seeking works.
- Single API server only (local disk). Moving to S3-compatible storage is the scaling step.

### RAG & Orchestration — LangChain + Gemini
- Package `langchain-google-genai`, key `GOOGLE_API_KEY`. Factories in `app/llm.py`:
  - `chat_model()` → `ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=0)` (default `gemini-2.5-flash`), used for the first claim pass.
  - `chat_model(strong=True)` → `GEMINI_MODEL_STRONG` (default `gemini-2.5-pro`), used for linking and Q&A.
  - `embeddings()` → `GoogleGenerativeAIEmbeddings(model=GEMINI_EMBEDDING_MODEL)` (default `models/gemini-embedding-001`).
- **Claim extraction chain:** `window_text (with [mm:ss] markers) → prompt → with_structured_output(ClaimBatch)`.
- **Linking chain:** `(new_claim, candidates[8]) → prompt → with_structured_output(LinkBatch)`. Also reused by `POST /claims/check`.
- **Q&A chain:** hybrid retrieval, Chroma top-12 windows + Postgres `websearch_to_tsquery` top-8 segments, merged by reciprocal rank fusion (k=60) → top 8 → answer with `[[window_id]]` citations → server-side citation check.
- Chroma is called with our own embeddings (`query_embeddings` / `embeddings=`), never with Chroma's built-in embedding function.

### Vector Store — ChromaDB
- `PersistentClient(path=CHROMA_PATH)` (default `./data/chroma`). Collections `transcript_windows` and `claims`, both filtered by `workspace_id` through one helper, `tenant_where(workspace_id, **conditions)`.
- Each collection records `embedding_model` in its metadata. At startup the API refuses to start if it differs from `GEMINI_EMBEDDING_MODEL` (rebuild with `POST /admin/reindex`).

### Data flow

```
React (Vite) ──(1) POST /episodes {show_id,title,published_at} ─▶ FastAPI ─▶ episodes(row, status=uploading)
     └──(2) POST /episodes/{id}/audio (multipart) ─▶ FastAPI ─▶ STORAGE_DIR/{ws}/{ep}.mp3, status=uploaded
     └──(3) POST /episodes/{id}/process ─▶ FastAPI ─▶ pipeline_jobs(stage=transcribe) ─▶ 202 {job_id}
                                                         │ BackgroundTask (worker role)
   ┌─────────────────────────────────────────────────────▼───────────────────────────────────────────┐
   │ transcribe  : file → ffmpeg 16 kHz mono temp WAV → ProcessPool(faster-whisper, en) → segments   │
   │ diarize?    : pyannote → speaker labels → update segments (skipped by default)                  │
   │ index       : segments → 45 s windows (10 s overlap) → embed → Chroma transcript_windows        │
   │ extract     : each window → LLM ClaimBatch → claims (start/end from snapped segment) → Chroma   │
   │ link        : each claim → Chroma claims (other, earlier episodes, top 8) → LLM LinkBatch → links │
   │ report      : SQL aggregate → episodes.report jsonb, status=ready                               │
   └─────────────────────────────────────────────────────────────────────────────────────────────────┘
   (each stage's job row is inserted when the previous stage finishes)
React ──(4) GET /jobs?episode_id ─ poll 3 s ─▶ stage progress
React ──(5) POST /ask {question} ─▶ FastAPI ─▶ Chroma windows ∪ Postgres FTS ─RRF─▶ LLM ─▶ answer + [mm:ss] links
React ──(6) GET /episodes/{id}/audio-url ─▶ signed /media URL ─▶ <audio> (Range requests) seeks to t
```

---

## 3. Database Schema & Vector Specifications

### 3.1 PostgreSQL DDL

All of this lives in `db/migrations/0001_init.sql` and is applied by `pcl_owner`. Roles `pcl_owner`, `pcl_app`, `pcl_worker` are created by `scripts/db_setup.py`.

```sql
create schema if not exists app;
create function app.current_user_id() returns uuid language sql stable as
  $$ select nullif(current_setting('app.user_id', true), '')::uuid $$;

-- ============ Auth ============
create table public.users (
  id            uuid primary key default gen_random_uuid(),
  email         text not null,
  password_hash text not null,
  display_name  text not null,
  is_active     boolean not null default true,
  created_at    timestamptz not null default now()
);
create unique index users_email_lower on public.users (lower(email));

create table public.refresh_tokens (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  token_hash  char(64) not null unique,           -- sha256 hex of the opaque token
  expires_at  timestamptz not null,
  revoked_at  timestamptz,
  replaced_by uuid references public.refresh_tokens(id),
  created_at  timestamptz not null default now()
);
create index refresh_tokens_user_idx on public.refresh_tokens(user_id);

-- ============ Tenancy ============
create table public.workspaces (
  id         uuid primary key default gen_random_uuid(),
  name       text not null check (char_length(name) between 1 and 100),
  created_at timestamptz not null default now()
);

create table public.workspace_members (
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  user_id      uuid not null references public.users(id) on delete cascade,
  role         text not null default 'viewer' check (role in ('owner','editor','viewer')),
  created_at   timestamptz not null default now(),
  primary key (workspace_id, user_id)
);
create index workspace_members_user_idx on public.workspace_members(user_id);

create table public.invites (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  email        text not null,
  role         text not null check (role in ('owner','editor','viewer')),
  token_hash   char(64) not null unique,
  invited_by   uuid references public.users(id) on delete set null,
  expires_at   timestamptz not null,               -- created_at + 7 days
  accepted_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index invites_workspace_idx on public.invites(workspace_id);

-- security definer: reads workspace_members as the table owner, so policies don't recurse
create function public.ws_role(p_ws uuid)
returns text language sql stable security definer set search_path = '' as $$
  select role from public.workspace_members
  where workspace_id = p_ws and user_id = (select app.current_user_id());
$$;

-- the only insert path for workspaces: creates the row and the caller's owner membership
create function public.create_workspace(p_name text)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_uid uuid := app.current_user_id(); v_id uuid;
begin
  if v_uid is null then raise exception 'create_workspace: no current user' using errcode = '42501'; end if;
  insert into public.workspaces(name) values (p_name) returning id into v_id;
  insert into public.workspace_members(workspace_id, user_id, role) values (v_id, v_uid, 'owner');
  return v_id;
end $$;

-- ============ Content ============
create table public.shows (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  title        text not null,
  default_speakers text[] not null default '{}',   -- e.g. {'Host: Priya','Host: Dan'}
  created_at   timestamptz not null default now()
);
create index shows_ws_idx on public.shows(workspace_id);

create table public.episodes (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references public.workspaces(id) on delete cascade,
  show_id       uuid not null references public.shows(id) on delete cascade,
  episode_no    int,
  title         text not null,
  published_at  date,
  storage_path  text,                  -- '{workspace_id}/{episode_id}.{ext}', relative to STORAGE_DIR; set when the upload completes
  duration_ms   int check (duration_ms > 0),
  status        text not null default 'uploading'
                check (status in ('uploading','uploaded','processing','ready','failed')),
  report        jsonb,                 -- pre-publish summary (§4 Report)
  created_at    timestamptz not null default now(),
  unique (show_id, episode_no)
);
create index episodes_show_pub_idx on public.episodes(show_id, published_at desc);
create index episodes_ws_idx on public.episodes(workspace_id);

create table public.transcript_segments (
  id           bigint generated always as identity primary key,
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  episode_id   uuid not null references public.episodes(id) on delete cascade,
  seq          int not null,
  start_ms     int not null check (start_ms >= 0),
  end_ms       int not null,
  speaker      text,
  text         text not null,
  avg_logprob  real,                   -- whisper confidence, used to mark unclear audio
  tsv          tsvector generated always as (to_tsvector('english', text)) stored,
  unique (episode_id, seq),
  check (end_ms > start_ms)
);
create index segments_episode_time_idx on public.transcript_segments(episode_id, start_ms);
create index segments_ws_idx on public.transcript_segments(workspace_id);
create index segments_tsv_idx on public.transcript_segments using gin (tsv);

create type public.claim_type as enum ('statistic','event','attribution','prediction','definition','opinion_flagged');

create table public.claims (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references public.workspaces(id) on delete cascade,
  episode_id    uuid not null references public.episodes(id) on delete cascade,
  window_id     text not null,          -- Chroma transcript_windows id
  speaker       text,
  claim_type    public.claim_type not null,
  normalized    text not null check (char_length(normalized) <= 400),  -- self-contained statement
  verbatim      text not null,          -- exact quote from transcript
  subject       text not null,          -- "Acme Corp"
  quantity      numeric,                -- 20000000 for "$20M"
  unit          text,                   -- "USD"
  as_of         date,                   -- the time the claim refers to, if stated
  start_ms      int not null,           -- snapped segment's start_ms
  end_ms        int not null,           -- snapped segment's end_ms
  verification  text not null default 'unreviewed'
                check (verification in ('unreviewed','sourced','corrected','retracted')),
  source_url    text,
  reviewed_by   uuid references public.users(id) on delete set null,
  created_at    timestamptz not null default now(),
  check (end_ms > start_ms)
);
create index claims_episode_idx on public.claims(episode_id, start_ms);
create index claims_ws_subject_idx on public.claims(workspace_id, lower(subject));
create index claims_ws_verification_idx on public.claims(workspace_id, verification);

create type public.link_relation as enum ('supports','contradicts','updates','duplicates');

create table public.claim_links (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references public.workspaces(id) on delete cascade,
  claim_id      uuid not null references public.claims(id) on delete cascade,   -- newer claim
  other_id      uuid not null references public.claims(id) on delete cascade,   -- earlier claim
  relation      public.link_relation not null,
  confidence    real not null check (confidence between 0 and 1),
  rationale     text not null check (char_length(rationale) <= 300),
  status        text not null default 'suggested' check (status in ('suggested','confirmed','rejected')),
  decided_by    uuid references public.users(id) on delete set null,
  created_at    timestamptz not null default now(),
  unique (claim_id, other_id),
  check (claim_id <> other_id)
);
create index claim_links_other_idx on public.claim_links(other_id);
create index claim_links_contradict_idx on public.claim_links(workspace_id, relation, status)
  where relation = 'contradicts';

-- human decisions saved before extract_claims deletes an episode's claims (§5 Phase 4)
create table public.preserved_claim_links (
  id             uuid primary key default gen_random_uuid(),
  workspace_id   uuid not null references public.workspaces(id) on delete cascade,
  episode_id     uuid not null references public.episodes(id) on delete cascade,  -- episode being re-processed
  verbatim       text not null,         -- the re-processed claim's verbatim
  start_ms       int not null,          -- and its start_ms (match window ± 2 s)
  counterpart_id uuid not null references public.claims(id) on delete cascade,    -- the claim in the other episode
  side           text not null check (side in ('claim','other')),  -- which side the re-processed claim was on
  relation       public.link_relation not null,
  confidence     real not null check (confidence between 0 and 1),
  rationale      text not null check (char_length(rationale) <= 300),
  status         text not null check (status in ('confirmed','rejected')),
  decided_by     uuid references public.users(id) on delete set null,
  created_at     timestamptz not null default now()
);
create index preserved_links_episode_idx on public.preserved_claim_links(episode_id);

create table public.pipeline_jobs (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  episode_id   uuid not null references public.episodes(id) on delete cascade,
  stage        text not null check (stage in ('transcribe','diarize','index','extract_claims','link_claims','report')),
  status       text not null default 'queued' check (status in ('queued','running','succeeded','failed','skipped')),
  progress     real not null default 0 check (progress between 0 and 1),
  error        text,
  started_at   timestamptz,
  finished_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index pipeline_jobs_episode_idx on public.pipeline_jobs(episode_id, created_at);
-- one row per stage, inserted when the previous stage finishes, so only one is ever active
create unique index pipeline_jobs_one_active on public.pipeline_jobs(episode_id)
  where status in ('queued','running');

-- ============ RLS ============
-- every table has RLS enabled; pcl_worker has BYPASSRLS; pcl_owner owns the tables and is not used at runtime
alter table public.users enable row level security;
alter table public.refresh_tokens enable row level security;   -- no app policies: auth routes use the worker role
alter table public.invites enable row level security;          -- no app policies: invite routes use the worker role
create policy users_self on public.users for select to pcl_app using (id = (select app.current_user_id()));

alter table public.workspaces enable row level security;
alter table public.workspace_members enable row level security;
create policy ws_read on public.workspaces for select to pcl_app using ((select public.ws_role(id)) is not null);
create policy ws_owner_update on public.workspaces for update to pcl_app
  using ((select public.ws_role(id)) = 'owner') with check ((select public.ws_role(id)) = 'owner');
create policy ws_members_read on public.workspace_members for select to pcl_app
  using ((select public.ws_role(workspace_id)) is not null);
create policy ws_members_owner on public.workspace_members for all to pcl_app
  using ((select public.ws_role(workspace_id)) = 'owner') with check ((select public.ws_role(workspace_id)) = 'owner');

do $$ declare t text; begin
  foreach t in array array['shows','episodes','transcript_segments','claims','claim_links',
                           'preserved_claim_links','pipeline_jobs']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format($f$create policy %1$s_read on public.%1$I for select to pcl_app
      using ((select public.ws_role(workspace_id)) is not null)$f$, t);
    execute format($f$create policy %1$s_write on public.%1$I for all to pcl_app
      using ((select public.ws_role(workspace_id)) in ('owner','editor'))
      with check ((select public.ws_role(workspace_id)) in ('owner','editor'))$f$, t);
  end loop;
end $$;

-- ============ Grants ============
-- pcl_app: only what its policies need. pcl_worker: full DML on every table.
-- functions: revoke the default PUBLIC execute; ws_role/create_workspace for pcl_app only.
```

The full grant list is in the migration. There are no storage policies: audio lives on local disk, and every file access goes through FastAPI (membership check, or a valid signature for `/media`).

### 3.2 ChromaDB collections

**`transcript_windows`**
```python
windows = client.create_collection(           # or get_collection + metadata check if it exists
    "transcript_windows",
    configuration={"hnsw": {"space": "cosine"}},
    metadata={"embedding_model": settings.GEMINI_EMBEDDING_MODEL, "schema_version": 1},
)
```
- **Chunking:** time-based windows aligned to Whisper segment edges. Segments are added until the window reaches ≥ 45 s; the next window starts at the first segment ≥ 35 s into the current one, giving ~10 s overlap. Windows never cut a segment in half. Hard cap: 2,000 characters per window.
- **Document text:** `"[{show_title} · Ep {episode_no} · {title} · {published_at}]\n[12:04] Priya: …\n[12:19] Guest: …"`. The bracketed `[mm:ss] speaker:` markers let the LLM cite times and attribute speakers.
- **ID:** `f"{episode_id}:w{index:05d}"`.
- **Metadata:** `workspace_id`, `show_id`, `episode_id`, `episode_no` (int, −1 if none), `published_at` (str ISO, `""` if none), `published_ts` (int unix, for range filters), `start_ms`, `end_ms`, `speakers_csv`, `low_confidence` (bool: mean `avg_logprob` < −1.0).

**`claims`**
- Same configuration and metadata keys as above.
- **One document per claim, not chunked:** `"{normalized}"` (≤ 400 chars). Embedding the normalized claim, not the verbatim speech, makes "we said they raised twenty million" and "Acme's Series B was $20M" land close together.
- **ID:** `claims.id`.
- **Metadata:** `workspace_id`, `show_id`, `episode_id`, `claim_type`, `subject_lc` (lowercased subject), `published_ts`, `has_quantity` (bool).
- **Candidate query for linking:** `query_embeddings=[embed(claim.normalized)]`, `n_results=8`, `where=tenant_where(ws, episode_id={"$ne": ep}, published_ts={"$lte": ep_published_ts})`. Candidates with cosine distance > 0.45 are dropped before the LLM call, so most claims never reach the LLM linker.
- `tenant_where(workspace_id, **conditions)` in `app/vectorstore.py` is the only way to build a `where`. It always includes `{"workspace_id": ws}` (ANDed with any other conditions) and raises on an empty workspace id.

### 3.3 Structured output models

```python
class ExtractedClaim(BaseModel):
    claim_type: Literal["statistic","event","attribution","prediction","definition","opinion_flagged"]
    normalized: str = Field(max_length=400, description="Self-contained; resolve pronouns; include the time referenced")
    verbatim: str = Field(max_length=600)
    subject: str = Field(max_length=120)
    quantity: Decimal | None = None
    unit: str | None = Field(default=None, max_length=20)
    as_of: date | None = None
    timestamp: str = Field(pattern=r"^\d{1,3}:\d{2}$")   # the [mm:ss] marker it came from
    speaker: str | None = None

class ClaimBatch(BaseModel):
    claims: list[ExtractedClaim] = Field(max_length=12)

class Link(BaseModel):
    other_id: UUID
    relation: Literal["supports","contradicts","updates","duplicates","unrelated"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=300)

class LinkBatch(BaseModel):
    links: list[Link]
```

---

## 4. API Specification

Base `/api/v1`. `Authorization: Bearer <access_token>` required except for `/health`, `/auth/*` and `/media/*` (signature instead). Times are in milliseconds. Errors are always `{"error":{"code":"snake_case","message":"…"}}` (422 → `validation_error`, 500 → `internal_error` with no stack trace).

### Auth (`/auth`, worker role)
| Method | Route | Request | Response |
|---|---|---|---|
| POST | `/auth/register` | `{"email","password"(10–128 chars),"display_name"}` | `201 {"user_id","email"}` · `409 email_taken` |
| POST | `/auth/login` | `{"email","password"}` | `{"access_token","token_type":"bearer","expires_in":900,"refresh_token"}` + `refresh_token` httpOnly cookie · `401 invalid_credentials` for any failure · 10/min per IP |
| POST | `/auth/refresh` | `{"refresh_token"}` or empty body with the cookie | new pair (old token revoked, `replaced_by` set) · reuse of a revoked token revokes all the user's tokens and returns 401 |
| POST | `/auth/logout` | `{"refresh_token"}` or the cookie | `204`, cookie cleared |
| POST | `/auth/accept-invite` | `{"token","password","display_name"}` | creates the user if the invite email has no account (an existing account must give its password), adds the membership, returns a token pair |

Access token: HS256 (`JWT_SECRET`, ≥ 32 bytes), claims `sub`, `email`, `iat`, `nbf`, `exp` (15 min), `iss="podcast-ledger"`, `aud="podcast-ledger-api"`, `typ="access"`. Refresh token: `secrets.token_urlsafe(32)`, stored as sha256 hex, 14 days, rotated on every use.

### Session & workspaces
| Method | Route | Request | Response |
|---|---|---|---|
| GET | `/health` | — | `{"status":"ok","db":"ok","chroma":"ok","whisper_model":"small.en"}` (503 with `"status":"degraded"` if a check fails) |
| GET | `/me` | — | `{"user_id","email","display_name","workspaces":[{"workspace_id","name","role"}]}` |
| POST | `/workspaces` | `{"name":"str"}` | `201 {"workspace_id","name","role":"owner"}` (calls `create_workspace`) |
| GET | `/workspaces` | — | `{"items":[{"workspace_id","name","role","created_at"}]}` (mine) |
| GET | `/workspaces/{id}/members` | — | `{"items":[{"user_id","email","display_name","role"}]}` · 404 if not a member |
| POST | `/workspaces/{id}/members` | `{"email","role":"editor"}` (owner only) | existing account → `201 {"user_id","role"}`; no account → `201 {"invite_token","expires_at"}` (shown once, 7 days; the owner shares the link `/accept-invite?token=…`; no email is sent in v1) · `409 already_member` |

### Ingestion & indexing
| Method | Route | Request | Response |
|---|---|---|---|
| POST | `/episodes` | `{"show_id","title","episode_no?","published_at?","filename":"ep212.mp3"}` | `201 {"episode_id","upload_url":"/api/v1/episodes/{id}/audio"}` (ext mp3/m4a/wav/mp4, else 422) |
| POST | `/episodes/{id}/audio` | multipart `file` (≤ 500 MB, MIME `audio/mpeg`, `audio/mp4`, `audio/x-m4a`, `audio/wav`, `audio/x-wav`, `video/mp4`) | `200 {"episode_id","storage_path","bytes"}`, status → `uploaded` · `413 file_too_large` · owner/editor |
| POST | `/episodes/{id}/process` | `{"diarize":false,"stages":null}` (`stages` re-runs a subset, e.g. `["link_claims"]`) | `202 {"job_id":"uuid"}` (the first stage's job; later rows appear as stages finish) · `409 conflict` if a job is active |
| POST | `/episodes/{id}/transcript-import` | `{"format":"vtt","content":"WEBVTT…"}` (skip Whisper if a transcript already exists) | `202 {"job_id"}` (the `diarize` job; `transcribe` is recorded as `succeeded`) |
| GET | `/jobs?episode_id=uuid` | — | `{"items":[{"job_id","stage","status","progress","error","started_at","finished_at"}]}` |
| PATCH | `/episodes/{id}/speakers` | `{"rename":{"SPEAKER_00":"Priya","SPEAKER_01":"Guest: Dr. Lee"}}` | `{"updated_segments":412}` (also updates claims and rebuilds the episode's Chroma windows) |
| POST | `/shows/{id}/process-backlog` | `{"diarize":false}` (owner/editor) | `202 {"episode_ids":[…]}`: every `uploaded` episode of the show, oldest `published_at` first (nulls ordered by `created_at`), processed one at a time so linking sees earlier episodes first · `409` if a backlog run is already active for the show |

### Vector query & retrieval
| Method | Route | Request | Response |
|---|---|---|---|
| POST | `/ask` | `{"workspace_id","question":"What did we say about Acme's funding?","show_id?":"uuid","date_from?":"2024-01-01"}` | `{"answer":"str","citations":[{"episode_id","episode_title","start_ms":724000,"speaker":"Priya","quote":"str"}],"insufficient_context":false}` |
| POST | `/search/moments` | `{"workspace_id","query":"str","mode":"hybrid","k":20}`; `mode` ∈ `hybrid` (Chroma + FTS, RRF k=60) \| `semantic` (Chroma only) \| `keyword` (FTS only) | `{"results":[{"episode_id","start_ms","end_ms","snippet","score","match":"semantic|keyword|both"}]}` |
| GET | `/claims/{claim_id}/related` | — | `{"claim":Claim,"links":{"contradicts":[LinkedClaim],"supports":[…],"updates":[…],"duplicates":[…]}}` (both directions) |
| POST | `/claims/check` | `{"workspace_id","text":"Acme raised $12M in its Series B"}` (pre-recording check of a script line) | `{"matches":[{"claim_id","normalized","relation","confidence","episode_title","start_ms"}]}` |

`/claims/check` reuses the linking chain: it embeds `text`, queries the 8 nearest claims with `tenant_where(ws)` (no episode or date filter), drops candidates with distance > 0.45, runs `LinkBatch` + `filter_links` + `numeric_override` (the text is treated as a new claim; its quantity/unit/subject come from one `ExtractedClaim` call), and writes nothing.

### Relational CRUD
| Method | Route | Request | Response |
|---|---|---|---|
| GET/POST | `/shows` | GET `?workspace_id`; POST `{"workspace_id","title","default_speakers":["Priya"]}` | `{"items":[Show]}` / `201 {"show_id"}` |
| GET | `/shows/{id}/episodes?cursor` | — | `{"items":[{"episode_id","title","status","published_at","contradictions":2}],"next_cursor"}` |
| GET | `/episodes/{id}` | — | `{"episode":Episode,"segments":[Segment],"claims":[Claim],"report":Report}` |
| GET | `/episodes/{id}/audio-url` | — | `{"url":"/api/v1/media/{ws}/{ep}.mp3?exp=…&sig=…","expires_in":600}` · 404 if no audio |
| GET | `/media/{workspace_id}/{file}?exp&sig` | — | the audio bytes (`FileResponse`, `Accept-Ranges: bytes`, 206 on Range) · 403 bad/expired signature · 404 bad path |
| GET | `/claims?workspace_id&type&verification&has_contradiction&cursor` | — | `{"items":[Claim],"next_cursor"}` |
| PATCH | `/claims/{id}` | `{"verification":"sourced","source_url":"https://…"}` or `{"normalized":"str"}` | `{"claim_id"}` (re-embeds if `normalized` changes) |
| PATCH | `/claim-links/{id}` | `{"status":"confirmed"}` | `{"link_id","status"}` |
| DELETE | `/episodes/{id}` | — | `204` (deletes rows, the audio file and Chroma docs `where=tenant_where(ws, episode_id=id)`) · 409 while a job is active |

### Admin
| Method | Route | Request | Response |
|---|---|---|---|
| POST | `/admin/reindex` | `{"workspace_id"}` (owner only) | `202 {"workspace_id","episodes":12}`; a background task (worker role) deletes the workspace's docs in both collections and rebuilds them from Postgres alone (windows from segments via `build_windows`, claims from `claims`) |

**Counting rules.** `contradictions` (episode list), `has_contradiction` (claims filter) and the report's `contradictions` count every `contradicts` link that is **not `rejected`**, i.e. both `suggested` and `confirmed`. Each report item carries its `status`, and the UI labels `suggested` ones "possible contradiction".

`Report` shape: `{"claims_total":34,"statistics_without_source":7,"contradictions":[{"link_id","claim_id","other_id","other_episode_title","other_start_ms","rationale","status"}],"duplicates":3,"low_confidence_minutes":2.5}`.
- `statistics_without_source`: claims with `claim_type='statistic'` and no `source_url`.
- `duplicates`: non-rejected `duplicates` links on the episode's claims.
- `low_confidence_minutes`: sum of `(end_ms - start_ms)` over the episode's segments with `avg_logprob < -1.0`, divided by 60,000, rounded to 0.1.

---

## 5. Implementation Roadmap

### Phase 1 — Environment & Schemas (days 1–3)
1. `web/` (Vite React TS + Tailwind v4 + TanStack Query), `api/` (FastAPI, `uv`, Python 3.12), `db/migrations/`, `scripts/db_setup.py`, `scripts/migrate.py`. Local prerequisites: PostgreSQL 16, Node 20+, uv, and `ffmpeg` on PATH for transcription (`winget install Gyan.FFmpeg`).
2. Migration `0001_init.sql` from §3.1 (all tables including auth, `preserved_claim_links`, RLS, `create_workspace`, grants).
3. pytest RLS tests on a real `podcast_ledger_test` database: cross-workspace reads return 0 rows on the app role; a viewer cannot patch claims; the worker role sees every workspace; `app/storage.py` rejects other workspaces' and malformed paths and bad signatures.
4. Fixtures: 3 short public-domain or self-recorded English clips (2–5 min) with a planted contradiction between them ("the bridge opened in 1932" vs "1937").

### Phase 2 — FastAPI & ChromaDB Core (days 4–12)
1. Auth (§4), `storage.py` (local disk, streaming upload, signed media URLs), the media route.
2. `pipeline/transcribe.py`: copy the stored file to a `tempfile`, run `ffmpeg -ac 1 -ar 16000` to normalize, then `WhisperModel(WHISPER_MODEL).transcribe(path, language="en", word_timestamps=True, vad_filter=True)` in `ProcessPoolExecutor(1)`. Write segments in batches of 200 and update `progress` = `seg.end / duration`.
3. `pipeline/windows.py`: a pure function `build_windows(segments) -> list[Window]`, unit-tested on edges (one long segment > 45 s, empty transcript, a 3-second episode).
4. `pipeline/claims.py`: extraction per window with 4 windows at a time. Map `timestamp` → ms, then snap to the last segment in the window with `start_ms ≤ t`; the claim takes that segment's `start_ms` **and `end_ms`**. Drop a claim if `verbatim` isn't found (fuzzy, `rapidfuzz.partial_ratio ≥ 85`) in that window's text.
5. `pipeline/link.py`: candidate query → skip if no candidates → LLM `LinkBatch` → keep links with `relation != unrelated` and `confidence ≥ 0.6`. Number check: if both claims have `quantity` and `unit` and the same `subject_lc`, a difference > 1% marks `contradicts` even if the LLM said `supports` (the LLM's rationale is kept, plus "numeric mismatch").
6. `/ask` with RRF fusion and a citation check (each `[[window_id]]` must be in the retrieved set).

**Exit check:** the fixture pipeline finds the planted contradiction with the right timestamps (± 3 s).

### Phase 3 — Frontend & Auth (days 13–18)
1. Login / register / accept-invite pages, in-memory access token + refresh cookie (§2). The workspace switcher stores the selected workspace in the URL.
2. Upload page: `POST /episodes`, then a multipart upload to `POST /episodes/{id}/audio` with an upload progress bar (`XMLHttpRequest` upload events), then stage-by-stage progress.
3. Episode page: transcript virtualized with `@tanstack/react-virtual` (2-hour episodes have ~1,500 segments), clicking a segment seeks the player, claim highlights.
4. Ledger and claim pages; the pre-publish report with "Confirm / Reject" on each suggested contradiction.
5. Ask page with clickable timestamp chips.

### Phase 4 — Integration & Edge Cases (days 19–24)
- **Back-catalog import:** `POST /shows/{id}/process-backlog` loops over the show's `uploaded` episodes oldest-first, so linking sees earlier episodes first. It runs one episode's pipeline at a time and the UI shows the queue.
- **Re-processing:** before re-running `extract_claims` deletes that episode's claims (cascading its links) and its Chroma claim docs, human-decided links (`confirmed` or `rejected`) touching those claims are copied to `preserved_claim_links`. During `link_claims`, a preserved row is re-attached (same relation, status and `decided_by`) when a new claim has a matching `verbatim` (`partial_ratio ≥ 85`) and `start_ms` within ± 2 s, and the preserved row is deleted.
- **Music/silence:** VAD removes it; windows with `low_confidence` are shown greyed and skipped for claim extraction.
- **Non-English audio:** out of scope; transcripts will be poor and the claim pass will mostly return nothing.
- **Publish date missing:** `published_ts` falls back to `created_at`, so ordering still works.
- **Evaluation:** 40 hand-labelled claim pairs. Target: precision ≥ 0.8 on `contradicts` (false alarms waste editors' time more than misses do).

---

## 6. Interview Talking Points & Architecture Trade-offs

### Key trade-offs
| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Frontend | Vite + React | Next.js | Logged-in dashboard, no SEO, heavy client state (player, virtualized transcript). A static SPA keeps the mental model small. |
| Auth | Own email/password + JWT (argon2id, rotating refresh tokens) | Hosted auth / OAuth | No external service to run locally; the RLS key is our own `app.user_id`. OAuth can be added later as another way to mint the same tokens. |
| Tenant isolation | Postgres RLS on a non-owner app role + a separate BYPASSRLS worker role | Filtering in application code only | A forgotten `where workspace_id` in a user request returns nothing instead of leaking. The worker role is the explicit, reviewable exception. |
| Transcription | Local `faster-whisper` (English-only `small.en`) in a process pool | Hosted speech API | No per-minute fees, works offline, word timestamps included. `.en` models are faster and more accurate for English than multilingual ones at the same size. Cost: CPU speed is ~1–2× real-time for `small.en`. `pipeline_jobs_one_active` gives one active job per episode, and the pool size of 1 keeps memory predictable. |
| Audio storage | Local disk, served by FastAPI with signed URLs and Range support | S3-compatible object storage + CDN | One process, no extra service. FastAPI does proxy the bytes, which is fine for an editorial team's traffic; move to object storage before running 2+ API instances. |
| Window size | 45 s with 10 s overlap | Sentence chunks | Claims often span several sentences and a speaker change. 45 s holds a full exchange, and overlap stops a claim at a boundary from being lost. |
| Contradiction detection | Retrieve 8 candidates → LLM classify | All-pairs comparison | All pairs is O(n²): 30k claims means 450M pairs. Retrieval caps it at 8 per new claim, and the distance cut-off means most claims trigger no LLM call. |
| Vector store | ChromaDB embedded | pgvector | Two collections with different metadata, and quick local iteration. Postgres keeps the truth (claims, links); Chroma is a rebuildable index (`POST /admin/reindex` regenerates it from Postgres). |
| Progress | Polling 3 s | SSE | A pipeline runs for minutes; polling job rows is simple and survives page reloads. |
| Hybrid search | Chroma + Postgres FTS, RRF | Vector only | Names and numbers ("Series B", "1937") are matched better by keywords. RRF needs no score calibration. |

### Hallucination mitigation & guardrails
1. **Verbatim check:** each claim's `verbatim` must fuzzy-match the window text, or it's dropped.
2. **Timestamp snapping:** the model gives a `[mm:ss]` marker from the input. The server snaps it to a real segment (its `start_ms` and `end_ms`), so the model never invents a time.
3. **Numeric override:** quantities are compared in code, not by the model.
4. **Human confirmation:** contradiction links start as `suggested`. The report says "possible contradiction" until an editor confirms it.
5. **Q&A refusal:** with no window above the similarity threshold (distance ≤ 0.5) and no keyword hit, the answer is `insufficient_context: true`.
6. **Prompt injection via audio:** transcript text is data inside delimiters, and the chains have no tools. A guest saying "ignore previous instructions" can at worst create a wrong suggestion that a human reviews.

### Latency optimization
- Transcription is the long pole (minutes). Everything after it is ~30–90 s per hour of audio with 4 concurrent LLM calls.
- The candidate distance cut-off removes ~70% of linker calls on typical catalogs.
- `/ask` p95 target < 4 s: embedding (~100 ms) + Chroma (~20 ms) + FTS (~10 ms) run concurrently, then one LLM call.
- Audio playback is served by FastAPI's `FileResponse` with HTTP Range, so the browser fetches only the bytes around the seek point instead of the whole file.

### Failure modes, rate limiting, scaling
- **Process crash mid-transcription:** on startup, `running` jobs older than 2× the episode duration are marked `failed`; the user clicks retry. Segments are deleted for that episode before a re-run, so there are no duplicates (`unique (episode_id, seq)` enforces it).
- **LLM 429s:** semaphore of 4 per process, exponential backoff with jitter, 3 tries. A failed window is recorded in `pipeline_jobs.error` and the stage still succeeds when ≥ 95% of windows succeeded.
- **API limits:** `slowapi`: `/auth/login` 10/min per IP; `/ask` 20/min per user; `/episodes` 30/hour per workspace.
- **Scaling path:** (1) move audio to S3-compatible storage (required before 2+ API instances); (2) move transcription to a separate GPU worker that claims `pipeline_jobs` with `for update skip locked` (the API code doesn't change); (3) Chroma server mode with `HttpClient`; (4) a collection per workspace once a workspace passes ~2M windows.
- **Storage costs:** after transcription, keep the original for playback but delete the 16 kHz temp file right away. An optional setting (`AUDIO_RETENTION_DAYS`) deletes audio after N days and keeps transcripts.

### Environment variables
| Name | Default (dev) | Meaning |
|---|---|---|
| `PG_SUPERUSER_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` | Used only by `scripts/db_setup.py` and the test suite |
| `MIGRATE_DATABASE_URL` | `postgresql://pcl_owner:pcl_owner@localhost:5432/podcast_ledger` | Owner role; migrations |
| `APP_DATABASE_URL` | `postgresql+asyncpg://pcl_app:pcl_app@localhost:5432/podcast_ledger` | Request role (RLS applies) |
| `WORKER_DATABASE_URL` | `postgresql+asyncpg://pcl_worker:pcl_worker@localhost:5432/podcast_ledger` | Background/auth role (BYPASSRLS) |
| `JWT_SECRET` | dev-only value ≥ 32 bytes | HS256 signing key; the API refuses to start if shorter |
| `ACCESS_TOKEN_TTL_SECONDS` | `900` | Access token lifetime |
| `REFRESH_TOKEN_TTL_SECONDS` | `1209600` | Refresh token lifetime (14 days) |
| `COOKIE_SECURE` | `false` | `Secure` flag on the refresh cookie (true in production) |
| `WEB_ORIGIN` | `http://localhost:3002` | The only CORS origin |
| `GOOGLE_API_KEY` | empty | Gemini key (read by `langchain-google-genai`) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Default chat model (claim extraction) |
| `GEMINI_MODEL_STRONG` | `gemini-2.5-pro` | Strong chat model (linking, Q&A) |
| `GEMINI_EMBEDDING_MODEL` | `models/gemini-embedding-001` | Embedding model; recorded in Chroma metadata |
| `LLM_CONCURRENCY` | `4` | Max concurrent LLM calls per process |
| `CHROMA_PATH` | `./data/chroma` | Chroma PersistentClient folder |
| `STORAGE_DIR` | `./data/storage` | Audio files root |
| `FILE_URL_SECRET` | dev-only value ≥ 32 bytes | HMAC key for signed media URLs |
| `WHISPER_MODEL` | `small.en` | English-only Whisper model (`medium.en`, `large-v3-turbo` are options; `language="en"` is always forced) |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` (CUDA 12 + cuDNN 9) |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` on CPU, `float16` on GPU |
| `HF_TOKEN` | empty | Only for the optional `diarize` extra (pyannote); diarization is off by default |
| `AUDIO_RETENTION_DAYS` | `0` | `0` keeps audio forever |
| `VITE_API_PROXY_TARGET` (web) | `http://localhost:8002` | Where the Vite dev server proxies `/api` |
