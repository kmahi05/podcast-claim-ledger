-- 0001_init: full schema from PRD.md §3.1. Applied as pcl_owner by scripts/migrate.py.
-- Roles pcl_owner / pcl_app / pcl_worker are created by scripts/db_setup.py.

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
-- pcl_app: only what its policies need. pcl_worker: full DML on every table (BYPASSRLS).
revoke all on all tables in schema public from public;
grant usage on schema public, app to pcl_app, pcl_worker;

grant select on public.users to pcl_app;
grant select, update on public.workspaces to pcl_app;
grant select, insert, update, delete on public.workspace_members to pcl_app;
grant select, insert, update, delete on
  public.shows, public.episodes, public.transcript_segments, public.claims,
  public.claim_links, public.preserved_claim_links, public.pipeline_jobs
  to pcl_app;

grant select, insert, update, delete on all tables in schema public to pcl_worker;
grant usage, select on all sequences in schema public to pcl_app, pcl_worker;

revoke execute on function public.ws_role(uuid), public.create_workspace(text) from public;
grant execute on function public.ws_role(uuid), public.create_workspace(text) to pcl_app;
grant execute on function app.current_user_id() to pcl_app, pcl_worker;
