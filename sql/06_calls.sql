-- ============================================================
-- 06_calls.sql
-- ============================================================

create table if not exists calls (
  id          uuid default gen_random_uuid() primary key,
  phone_id    uuid references phones(id) on delete cascade,
  contact_id  uuid references contacts(id) on delete set null,
  scenario_id uuid references scenarios(id) on delete set null,

  status      text not null,   -- success | failed | stuck
  started_at  timestamp,
  ended_at    timestamp,

  created_at  timestamp default now()
);

create index if not exists calls_phone_id_idx   on calls(phone_id);
create index if not exists calls_contact_id_idx on calls(contact_id);
