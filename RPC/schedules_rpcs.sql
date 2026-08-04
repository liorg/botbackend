-- ============================================================
-- RPC: schedules_list — רשימת תזמונים + סטטוס call אחרון
-- קריאה אחת במקום schedules + calls (מונע roundtrip כפול).
-- ============================================================

create or replace function schedules_list(
  p_phone_id uuid default null,
  p_status   text default null
)
returns jsonb
language sql
stable
as $fn$
  select coalesce(
    jsonb_agg(to_jsonb(row) order by row.created_at desc),
    '[]'::jsonb
  )
  from (
    select
      s.id,
      s.user_id,
      s.phone_id,
      s.contact_id,
      s.scenario_id,
      s.schedule_name,
      s.schedule_type,
      s.cron_expr,
      s.run_at,
      s.next_run,
      s.last_run,
      s.status,
      s.created_at,
      s.updated_at,
      jsonb_build_object('name', sc.name)      as scenarios,
      lc.status                                as last_call_status,
      (lc.status = 'running')                  as running
    from schedules s
    left join scenarios sc on sc.id = s.scenario_id
    left join lateral (
      select c.status
      from calls c
      where c.schedule_id = s.id
      order by c.created_at desc
      limit 1
    ) lc on true
    where (p_phone_id is null or s.phone_id = p_phone_id)
      and (p_status   is null or s.status   = p_status)
  ) row;
$fn$;


-- ============================================================
-- RPC: schedule_get — תזמון בודד, אותו shape בדיוק
-- (משמש את ה-poll של כפתור ה-Play כל 5 שניות)
-- ============================================================

create or replace function schedule_get(p_schedule_id uuid)
returns jsonb
language sql
stable
as $fn$
  select to_jsonb(row)
  from (
    select
      s.id,
      s.user_id,
      s.phone_id,
      s.contact_id,
      s.scenario_id,
      s.schedule_name,
      s.schedule_type,
      s.cron_expr,
      s.run_at,
      s.next_run,
      s.last_run,
      s.status,
      s.created_at,
      s.updated_at,
      jsonb_build_object('name', sc.name)      as scenarios,
      lc.status                                as last_call_status,
      (lc.status = 'running')                  as running
    from schedules s
    left join scenarios sc on sc.id = s.scenario_id
    left join lateral (
      select c.status
      from calls c
      where c.schedule_id = s.id
      order by c.created_at desc
      limit 1
    ) lc on true
    where s.id = p_schedule_id
  ) row;
$fn$;


-- אינדקס שה-lateral נשען עליו (אם לא קיים כבר):
create index if not exists idx_calls_schedule_created
  on calls (schedule_id, created_at desc);
