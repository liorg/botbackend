-- ============================================================
-- executions module — migration v2 (מיושר לסכמה האמיתית)
--   contacts.number · phone_id uuid בכל מקום · schedules.next_run
-- ============================================================



-- ============================================================
-- RPC 1 — seek: contact active לפי contacts.number
-- ============================================================
CREATE OR REPLACE FUNCTION exec_seek_contact(p_phone_number text)
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT jsonb_build_object(
    'contact_phone_number', p_phone_number,
    'contact_name', (SELECT c.name FROM contacts c
                     WHERE c.number = p_phone_number AND c.tag = 'active'
                     LIMIT 1),
    'phones', COALESCE(jsonb_agg(jsonb_build_object(
        'phone_id',     c.phone_id,
        'phone_number', p.number,
        'contact_id',   c.id,
        'contact_name', c.name,
        'phone_status', p.status
      ) ORDER BY p.number), '[]'::jsonb)
  )
  FROM contacts c
  JOIN phones p ON p.id = c.phone_id
  WHERE c.number = p_phone_number
    AND c.tag = 'active';
$$;

-- ============================================================
-- RPC 2 — תרחישים active לטלפונים שנבחרו
-- ============================================================
CREATE OR REPLACE FUNCTION exec_list_scenarios(p_phone_ids uuid[])
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id',           s.id,
      'name',         s.name,
      'phone_id',     s.phone_id,
      'phone_number', p.number,
      'description',  s.config->>'description'
    ) ORDER BY s.name), '[]'::jsonb)
  FROM scenarios s
  JOIN phones p ON p.id = s.phone_id
  WHERE s.phone_id = ANY(p_phone_ids)
    AND s.status = 'active';
$$;

-- ============================================================
-- RPC 3 — יצירה אטומית: execution + שכפולים + schedules(once) + links
--   p_targets: [{"phone_id":"uuid","contact_id":"uuid"}]
-- ============================================================
CREATE OR REPLACE FUNCTION exec_create(
  p_name                 text,
  p_contact_phone_number text,
  p_source_scenario_id   uuid,
  p_run_mode             text,
  p_run_at               timestamptz,
  p_targets              jsonb
)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_exec_id      uuid;
  v_src          scenarios%ROWTYPE;
  v_t            jsonb;
  v_new_scen_id  uuid;
  v_sched_id     uuid;
  v_links        jsonb := '[]'::jsonb;
BEGIN
  SELECT * INTO v_src FROM scenarios WHERE id = p_source_scenario_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'source scenario % not found', p_source_scenario_id;
  END IF;

  INSERT INTO executions (name, contact_phone_number, contact_name,
                          source_scenario_id, run_mode, status)
  VALUES (p_name, p_contact_phone_number,
          (SELECT name FROM contacts
             WHERE number = p_contact_phone_number AND tag='active' LIMIT 1),
          p_source_scenario_id, p_run_mode,
          CASE WHEN p_run_mode = 'once' THEN 'scheduled' ELSE 'stopped' END)
  RETURNING id INTO v_exec_id;

  FOR v_t IN SELECT * FROM jsonb_array_elements(p_targets)
  LOOP
    INSERT INTO scenarios (phone_id, contact_id, name, status, config,
                           event_type, priority,
                           estimated_duration_minutes, inter_leaf_response_time,
                           source_scenario_id)
    VALUES ((v_t->>'phone_id')::uuid,
            (v_t->>'contact_id')::uuid,
            v_src.name || ' from ' || p_name,
            'active',
            v_src.config,
            'scheduler',
            v_src.priority,
            v_src.estimated_duration_minutes,
            v_src.inter_leaf_response_time,
            p_source_scenario_id)
    RETURNING id INTO v_new_scen_id;

    v_sched_id := NULL;
    IF p_run_mode = 'once' THEN
      INSERT INTO schedules (phone_id, contact_id, scenario_id, schedule_name,
                             schedule_type, status, run_at, next_run)
      VALUES ((v_t->>'phone_id')::uuid,
              (v_t->>'contact_id')::uuid,
              v_new_scen_id,
              p_name,
              'once', 'active', p_run_at, p_run_at)
      RETURNING id INTO v_sched_id;
    END IF;

    INSERT INTO execution_links (execution_id, phone_id, contact_id,
                                 scenario_id, schedule_id)
    VALUES (v_exec_id, (v_t->>'phone_id')::uuid, (v_t->>'contact_id')::uuid,
            v_new_scen_id, v_sched_id);

    v_links := v_links || jsonb_build_object(
      'phone_id', v_t->>'phone_id',
      'scenario_id', v_new_scen_id,
      'schedule_id', v_sched_id);
  END LOOP;

  RETURN jsonb_build_object('execution_id', v_exec_id, 'links', v_links);
END;
$$;

-- ============================================================
-- RPC 4 — רשימה מועשרת: chips = phones.number
-- ============================================================
CREATE OR REPLACE FUNCTION exec_list()
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(row ORDER BY row->>'created_at' DESC), '[]'::jsonb)
  FROM (
    SELECT jsonb_build_object(
      'id', e.id,
      'name', e.name,
      'contact_phone_number', e.contact_phone_number,
      'contact_name', e.contact_name,
      'source_scenario_id', e.source_scenario_id,
      'source_scenario_name', ss.name,
      'run_mode', e.run_mode,
      'status', e.status,
      'created_at', e.created_at,
      'phones', (SELECT COALESCE(jsonb_agg(p.number ORDER BY p.number), '[]'::jsonb)
                 FROM execution_links l
                 JOIN phones p ON p.id = l.phone_id
                 WHERE l.execution_id = e.id),
      'run_at', (SELECT min(s.run_at) FROM execution_links l
                 JOIN schedules s ON s.id = l.schedule_id
                 WHERE l.execution_id = e.id)
    ) AS row
    FROM executions e
    LEFT JOIN scenarios ss ON ss.id = e.source_scenario_id
  ) t;
$$;

-- ============================================================
-- RPC 5 — links של ביצוע
-- ============================================================
CREATE OR REPLACE FUNCTION exec_links(p_execution_id uuid)
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'phone_id', l.phone_id,
      'contact_id', l.contact_id,
      'scenario_id', l.scenario_id,
      'schedule_id', l.schedule_id)), '[]'::jsonb)
  FROM execution_links l
  WHERE l.execution_id = p_execution_id;
$$;
