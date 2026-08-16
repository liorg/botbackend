-- rpc_list_contact_calls.sql — להריץ ב-Supabase SQL Editor
-- רשימת שיחות של איש קשר עם דפדוף. כל לוגיקת הדפדוף בתוך ה-RPC.
-- גודל עמוד נלקח מ-bot_config בערך 'contact_call.paging' (ברירת מחדל 20).

-- ── ערך ברירת מחדל ל-bot_config ────────────────────────────────────────────
insert into bot_config (key, value)
values ('contact_call.paging', '20')
on conflict (key) do nothing;


create or replace function rpc_list_contact_calls(
  p_contact_id uuid,
  p_page       integer default 1,
  p_status     text    default null,
  p_page_size  integer default null   -- null ⇒ נלקח מ-bot_config
)
returns jsonb
language plpgsql
stable
as $$
#variable_conflict use_column
declare
  v_page_size integer;
  v_page      integer := greatest(coalesce(p_page, 1), 1);
  v_offset    integer;
  v_total     bigint;
  v_rows      jsonb;
begin
  -- גודל עמוד: פרמטר מפורש ← bot_config ← ברירת מחדל
  if p_page_size is not null and p_page_size > 0 then
    v_page_size := p_page_size;
  else
    select nullif(bc.value, '')::integer
      into v_page_size
      from bot_config bc
     where bc.key = 'contact_call.paging';
    v_page_size := coalesce(v_page_size, 20);
  end if;

  v_offset := (v_page - 1) * v_page_size;

  -- סה"כ שורות (לפני דפדוף)
  select count(*)
    into v_total
    from calls c
   where c.contact_id = p_contact_id
     and (p_status is null or c.status = p_status);

  -- העמוד המבוקש
  select coalesce(jsonb_agg(row_json order by ord), '[]'::jsonb)
    into v_rows
    from (
      select
        row_number() over (order by c.created_at desc) as ord,
        jsonb_build_object(
          'id',               c.id,
          'phone_id',         c.phone_id,
          'contact_id',       c.contact_id,
          'scenario_id',      c.scenario_id,
          'status',           c.status,
          'started_at',       c.started_at,
          'ended_at',         c.ended_at,
          'created_at',       c.created_at,
          'duration_seconds', c.duration_seconds,
          'source',           c.source,
          'call_type',        c.call_type,
          'priority',         c.priority,
          'sender_count',     c.sender_count,
          'expected_count',   c.expected_count,
          'mismatch_count',   c.mismatch_count,
          'last_step_id',     c.last_step_id,
          'scenarios', case when s.id is null then null else jsonb_build_object(
            'id',         s.id,
            'name',       s.name,
            'event_type', s.event_type,
            'priority',   s.priority
          ) end
        ) as row_json
      from calls c
      left join scenarios s on s.id = c.scenario_id
      where c.contact_id = p_contact_id
        and (p_status is null or c.status = p_status)
      order by c.created_at desc
      limit v_page_size offset v_offset
    ) t;

  return jsonb_build_object(
    'calls',       v_rows,
    'page',        v_page,
    'page_size',   v_page_size,
    'total',       v_total,
    'total_pages', greatest(ceil(v_total::numeric / v_page_size)::integer, 1),
    -- הסטטוסים הקיימים לאיש הקשר (לצ'יפים של הסינון) — לא מושפע מ-p_status
    'statuses', coalesce((
      select jsonb_agg(distinct c2.status)
      from calls c2
      where c2.contact_id = p_contact_id and c2.status is not null
    ), '[]'::jsonb)
  );
end;
$$;
