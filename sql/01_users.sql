-- ============================================================
-- 01_users.sql
-- ============================================================

create table if not exists users (
  id         uuid default gen_random_uuid() primary key,
  email      text unique not null,
  name       text,
  google_id  text unique not null,
  avatar     text,
  last_login timestamp,
  created_at timestamp default now()
);
