create or replace function spine_call_events(p_call_id uuid)
returns jsonb
language sql
stable
as $fn$
  select coalesce(
    (
      select jsonb_agg(to_jsonb(e) order by e."timestamp" asc, e.id asc)
      from (
        select
          id,
          event_type,
          step_id,
          step_type,
          data,
          "timestamp"
        from spine_events
        where call_id = p_call_id
      ) e
    ),
    '[]'::jsonb
  );
$fn$;
