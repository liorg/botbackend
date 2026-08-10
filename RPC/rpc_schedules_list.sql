
create or replace function rpc_schedules_list(
  p_phone_id uuid default null,
  p_status   text default null,
  p_page     int  default null      -- null = ללא דפדוף (תאימות לישן)
) returns jsonb
language plpgsql stable as $$
-- plpgsql ולא sql: ב-LIMIT/OFFSET אסור להתייחס לעמודה מ-FROM
-- ("argument of OFFSET must not contain variables"), אבל משתנה
-- מקומי של plpgsql עובר כפרמטר ולכן מותר.
declare
  v_size int := least(greatest(bot_config_int('schedules_page_size', 20), 1), 200);
  v_lim  int := case when p_page is null then null else v_size end;
  v_off  int := case when p_page is null then 0
                     else (greatest(p_page, 1) - 1) * v_size end;
  v_out  jsonb;
begin
  with base as (
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
      jsonb_build_object('name', sc.name)              as scenarios,
      lc.status                                        as last_call_status,
      coalesce(lc.status = 'running', false)           as running
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
  ),
  pg as (
    select b.*
    from base b
    order by b.created_at desc
    limit  v_lim          -- null → כל השורות
    offset v_off
  )
  select jsonb_build_object(
    'schedules', coalesce(
                   (select jsonb_agg(to_jsonb(x) order by x.created_at desc) from pg x),
                   '[]'::jsonb),
    'total',     (select count(*) from base),
    'page',      greatest(coalesce(p_page, 1), 1),
    'page_size', v_size
  )
  into v_out;
 
  return v_out;
end $$;
 
