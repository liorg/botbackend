-- ============================================================
-- 04_scenarios.sql
-- ============================================================

create table if not exists scenarios (
  id         uuid default gen_random_uuid() primary key,
  phone_id   uuid references phones(id) on delete cascade,
  contact_id uuid references contacts(id) on delete set null,

  name       text not null,
  status     text default 'draft',   -- draft | active | archived

  -- התרחיש עצמו - מערך steps
  -- דוגמה:
  -- {
  --   "steps": [
  --     { "id": "s1", "type": "text", "text": "שלום!" },
  --     { "id": "s2", "type": "menu", "header": "בחר נושא",
  --       "sections": [{ "title": "שירותים",
  --         "rows": [{ "id": "r1", "title": "תמיכה", "next_step": "s3" }] }] },
  --     { "id": "s3", "type": "buttons", "body": "דחיפות?",
  --       "buttons": [{ "id": "b1", "title": "דחוף", "next_step": "s4" }] }
  --   ]
  -- }
  config     jsonb not null default '{}',

  created_at timestamp default now()
);

create index if not exists scenarios_phone_id_idx   on scenarios(phone_id);
create index if not exists scenarios_contact_id_idx on scenarios(contact_id);
