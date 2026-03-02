-- ============================================================
-- 03_contacts.sql
-- ============================================================

create table if not exists contacts (
  id         uuid default gen_random_uuid() primary key,
  phone_id   uuid references phones(id) on delete cascade,

  lid        text,            -- מזהה WhatsApp lid
  number     text not null,   -- מספר טלפון של איש הקשר
  name       text,
  email      text,
  avatar     text,
  tag        text,
  is_bot     boolean default false,  -- true = bot contact

  created_at timestamp default now(),
  unique(phone_id, number)
);

create index if not exists contacts_phone_id_idx on contacts(phone_id);
create index if not exists contacts_is_bot_idx   on contacts(phone_id, is_bot);
