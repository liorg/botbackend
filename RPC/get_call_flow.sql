-- get_call_flow.sql — להריץ ב-Supabase SQL Editor
-- RPC אחד שמחזיר את כל ה-flow: call + scenario + snapshot + עלים עם ההודעות שלהם
-- הקשר עלה↔הודעה דרך טבלת ה-N:N spine_leaf_messages

create or replace function get_call_flow(p_contact_id uuid, p_call_id uuid)
returns jsonb
language sql
stable
as $$
select jsonb_build_object(

  -- ── פרטי השיחה + התרחיש ──────────────────────────────────────────────
  'call', (
    select jsonb_build_object(
      'id',              c.id,
      'phone_id',        c.phone_id,
      'contact_id',      c.contact_id,
      'scenario_id',     c.scenario_id,
      'status',          c.status,
      'started_at',      c.started_at,
      'ended_at',        c.ended_at,
      'created_at',      c.created_at,
      'expected_end',    c.expected_end,
      'duration_seconds',c.duration_seconds,
      'source',          c.source,
      'call_type',       c.call_type,
      'priority',        c.priority,
      'sender_count',    c.sender_count,
      'expected_count',  c.expected_count,
      'mismatch_count',  c.mismatch_count,
      'last_step_id',    c.last_step_id,
      'variables',       c.variables,
      'scenarios', case when s.id is null then null else jsonb_build_object(
        'id',                         s.id,
        'name',                       s.name,
        'event_type',                 s.event_type,
        'priority',                   s.priority,
        'inter_leaf_response_time',   s.inter_leaf_response_time,
        'estimated_duration_minutes', s.estimated_duration_minutes
      ) end
    )
    from calls c
    left join scenarios s on s.id = c.scenario_id
    where c.id = p_call_id and c.contact_id = p_contact_id
  ),

  -- ── ה-snapshot כפי שנשמר ─────────────────────────────────────────────
  'snapshot', (
    select c.scenario_snapshot
    from calls c
    where c.id = p_call_id and c.contact_id = p_contact_id
  ),

  -- ── העלים + ההודעה האחרונה (retry הגבוה ביותר) דרך spine_leaf_messages ──
  'leaves', (
    select coalesce(jsonb_agg(leaf order by leaf->>'timestamp'), '[]'::jsonb)
    from (
      select to_jsonb(sl) || jsonb_build_object(
        'message', (
          select jsonb_build_object(
            'id',                  m.id,
            'retry_counter',       m.retry_counter,
            'whatsapp_message_id', m.whatsapp_message_id,
            'status',              m.status,
            'sent_at',             m.sent_at,
            'direction',           m.direction
          )
          from spine_leaf_messages slm
          join messages m on m.id = slm.message_id
          where slm.leaf_id = sl.leaf_id
          order by m.retry_counter desc nulls last, m.sent_at desc
          limit 1
        ),
        'attempts', (
          select count(*)
          from spine_leaf_messages slm
          where slm.leaf_id = sl.leaf_id
        )
      ) as leaf
      from spine_leaves sl
      where sl.call_id = p_call_id
    ) t
  )
);
$$;
