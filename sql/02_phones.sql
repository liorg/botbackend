-- ============================================================
-- 02_phones.sql
-- ============================================================

create table if not exists phones (
  id            uuid default gen_random_uuid() primary key,
  user_id       uuid references users(id) on delete cascade,
  number        text not null,
  label         text,
  color         text default '#4A90E2',
  status        text default 'active',        -- active | disconnected

  -- Docker instance on Oracle Cloud VM
  docker_url    text,                          -- http://oracle-vm-ip:PORT
  docker_status text default 'unknown',        -- running | stopped | unknown

  created_at    timestamp default now(),
  unique(user_id, number)
);

create index if not exists phones_user_id_idx on phones(user_id);
