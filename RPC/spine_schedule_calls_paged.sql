create or replace function spine_schedule_calls_paged(
  p_schedule_id uuid,
  p_limit       int default 20,
  p_offset      int default 0
)
returns jsonb
language sql
stable
as $fn$
  with base as (
    select
      c.id                                   as call_id,
      c.status,
      c.source,
      c.started_at,
      c.ended_at,
      c.created_at,
      c.duration_seconds,
      c.last_step_id,
      c.mismatch_count,
      ct.name                                as contact_name,
      ct.number                              as contact_phone,
      sc.name                                as scenario_name,
      coalesce((
        select count(*)::int from spine_leaves sl
        where sl.call_id = c.id
      ), 0)                                  as leaves_total,
      coalesce((
        select count(*)::int from spine_leaves sl
        where sl.call_id = c.id and sl.status = 'sent'
      ), 0)                                  as leaves_sent,
      coalesce((
        select count(*)::int from spine_leaves sl
        where sl.call_id = c.id and sl.status = 'failed'
      ), 0)                                  as leaves_failed
    from calls c
    left join contacts  ct on ct.id = c.contact_id
    left join scenarios sc on sc.id = c.scenario_id
    where c.schedule_id = p_schedule_id
  )
  select jsonb_build_object(
    'total', (select count(*) from base),
    'calls', coalesce(
      (
        select jsonb_agg(to_jsonb(b))
        from (
          select * from base
          order by coalesce(started_at, created_at) desc
          limit p_limit offset p_offset
        ) b
      ),
      '[]'::jsonb
    )
  );
$fn$;
