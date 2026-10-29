create table if not exists public.roof_building_search_events (
  id bigint generated always as identity primary key,
  address text not null check (char_length(address) between 1 and 300),
  searched_at timestamptz not null default now()
);
alter table public.roof_building_search_events enable row level security;
revoke all on public.roof_building_search_events from anon, authenticated;
grant select, insert on public.roof_building_search_events to service_role;
grant usage, select on sequence public.roof_building_search_events_id_seq to service_role;
