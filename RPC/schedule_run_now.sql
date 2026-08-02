-- RPC: schedule_run_now — "Play" למסך הביצועים (שבוע הבא)
--
-- לעולם לא דרך FastAPI — React קורא ישירות:
--   supabase.rpc('schedule_run_now', { p_schedule_id })
--
-- התנהגות זהה ל-Play של מסך התזמונים:
--   next_run = now(), status = 'active'
-- ה-Spine Scheduler (היוזם היחיד) תופס בסבב הבא.
--
-- סוגים מותרים: once / manual בלבד (סוגי מסך הביצועים).
-- manual שכבר הושלם — סגור; ירה פעם אחת בלבד.

create or replace function schedule_run_now(p_schedule_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row schedules%rowtype;
begin
  select * into v_row
  from schedules
  where id = p_schedule_id
    and user_id = auth.uid()
  for update;

  if not found then
    return jsonb_build_object('scheduled', false, 'error', 'not_found');
  end if;

  if v_row.schedule_type not in ('once', 'manual') then
    return jsonb_build_object(
      'scheduled', false,
      'error', 'type_not_runnable',
      'schedule_type', v_row.schedule_type
    );
  end if;

  -- ה-Scheduler באמצע ירי — לא נוגעים (כמו 409 ב-API)
  if v_row.status = 'firing' then
    return jsonb_build_object('scheduled', false, 'error', 'firing');
  end if;

  -- manual יורה פעם אחת בלבד
  if v_row.schedule_type = 'manual' and v_row.status = 'completed' then
    return jsonb_build_object('scheduled', false, 'error', 'already_ran');
  end if;

  if v_row.phone_id is null or v_row.scenario_id is null then
    return jsonb_build_object('scheduled', false, 'error', 'missing_phone_or_scenario');
  end if;

  -- כבר ממתין לסבב הקרוב — לא דורסים
  if v_row.status = 'active'
     and v_row.next_run is not null
     and v_row.next_run <= now() then
    return jsonb_build_object('scheduled', true, 'already_pending', true);
  end if;

  update schedules
  set status     = 'active',
      next_run   = now(),
      updated_at = now()
  where id = p_schedule_id;

  return jsonb_build_object('scheduled', true, 'already_pending', false);
end;
$$;

grant execute on function schedule_run_now(uuid) to authenticated;
