-- ─────────────────────────────────────────────────────────────────────────────
-- rpc_schedules_list — רשימת תזמונים לגריד, עם דפדוף בצד שרת.
--
-- שם חדש בקונבנציה rpc_* : כל פונקציה שנקראת מהראוטר דרך sb.rpc() נושאת
-- את התחילית, כדי שיהיה אפשר להבדיל במבט אחד בין RPC של ה-API לבין
-- פונקציות פנימיות (bot_config_int) ולוגיקת Spine (spine_ensure_call).
--
-- מחליפה את schedules_list הישן. הגוף זהה, בתוספת:
--   p_page = null → כל השורות (התנהגות הישן)
--   p_page = N    → עמוד N, גודל העמוד נגזר מ-bot_config בתוך ה-RPC
-- וההחזרה היא אובייקט {schedules, total, page, page_size} במקום מערך.
--
-- מיגרציה: יוצרים חדש → מעדכנים את הראוטר → מוודאים → מוחקים ישן.
--   grep -rn "schedules_list" /opt/ICR      -- לאתר קוראים נוספים
--   drop function if exists schedules_list(uuid, text);   -- רק אחרי אימות
-- ─────────────────────────────────────────────────────────────────────────────

insert into bot_config(key, value, description)
values ('schedules_page_size', '20', 'גודל עמוד בגריד התזמונים')
on conflict (key) do nothing;


create or replace function rpc_schedules_list(
  p_phone_id uuid default null,
  p_status   text default null,
  p_page     int  default null      -- null = ללא דפדוף (תאימות לישן)
) returns jsonb
language sql stable as $$
  with cfg as (
    select least(greatest(bot_config_int('schedules_page_size', 20), 1), 200) as size
  ),
  base as (
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
    select r.*
    from base r, cfg
    order by r.created_at desc
    -- p_page null → כל השורות; אחרת חלון של size
    limit  case when p_page is null then null else cfg.size end
    offset case when p_page is null then 0
                else (greatest(p_page, 1) - 1) * cfg.size end
  )
  select jsonb_build_object(
    'schedules', coalesce(
                   (select jsonb_agg(to_jsonb(x) order by x.created_at desc) from pg x),
                   '[]'::jsonb),
    'total',     (select count(*) from base),
    'page',      greatest(coalesce(p_page, 1), 1),
    'page_size', (select size from cfg)
  );
$$;
