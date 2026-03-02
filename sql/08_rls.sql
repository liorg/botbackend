-- ============================================================
-- 08_rls.sql  —  Row Level Security
-- הרץ רק אם אתה משתמש ב-Supabase Auth ישירות מהפרונטאנד.
-- אם כל הגישה דרך FastAPI עם service_role key — לא חובה.
-- ============================================================

alter table phones    enable row level security;
alter table contacts  enable row level security;
alter table scenarios enable row level security;
alter table schedules enable row level security;
alter table calls     enable row level security;
alter table messages  enable row level security;

-- phones: כל משתמש רואה רק את הטלפונים שלו
create policy "own phones" on phones
  for all using (user_id = auth.uid());

-- contacts: דרך הטלפון
create policy "own contacts" on contacts
  for all using (
    phone_id in (select id from phones where user_id = auth.uid())
  );

-- scenarios
create policy "own scenarios" on scenarios
  for all using (
    phone_id in (select id from phones where user_id = auth.uid())
  );

-- schedules
create policy "own schedules" on schedules
  for all using (
    phone_id in (select id from phones where user_id = auth.uid())
  );

-- calls
create policy "own calls" on calls
  for all using (
    phone_id in (select id from phones where user_id = auth.uid())
  );

-- messages: דרך השיחה
create policy "own messages" on messages
  for all using (
    call_id in (
      select c.id from calls c
      join phones p on p.id = c.phone_id
      where p.user_id = auth.uid()
    )
  );
