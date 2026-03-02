-- ============================================================
-- 05_schedules.sql
-- ============================================================

create table if not exists schedules (
  id            uuid default gen_random_uuid() primary key,
  phone_id      uuid references phones(id) on delete cascade,
  contact_id    uuid references contacts(id) on delete set null,
  scenario_id   uuid references scenarios(id) on delete cascade,

  schedule_name text,
  schedule_type text not null,        -- once | daily | weekly | interval
  status        text default 'ready', -- ready | running | disabled

  -- תזמון
  run_at        timestamp,            -- לתזמון חד-פעמי
  cron_expr     text,                 -- לתזמון חוזר  e.g. "0 9 * * 1"
  interval_min  int,                  -- לתזמון לפי דקות

  last_run      timestamp,
  next_run      timestamp,

  created_at    timestamp default now()
);

create index if not exists schedules_phone_id_idx    on schedules(phone_id);
create index if not exists schedules_scenario_id_idx on schedules(scenario_id);
