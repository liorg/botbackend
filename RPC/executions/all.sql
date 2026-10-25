-- ============================================================
-- executions module — migration v3
--   טלפון מקור → שכפול תרחיש + תבניות לכל טלפון יעד
--   תרחיש:  "{run} {scenario}"
--   תבנית:  "{prefix}_{template}"   status=approved, is_published=true (baileys)
--   תזמון:  "{run} JOB"              schedule_type=manual, next_run=NULL
--   הפעלה:  exec_run מציב next_run=now() לכל התזמונים → ה-Scheduler יורה במקביל
-- ============================================================

ALTER TABLE executions ADD COLUMN IF NOT EXISTS template_prefix text;

CREATE TABLE IF NOT EXISTS execution_templates (
  execution_id       uuid NOT NULL REFERENCES executions(id)      ON DELETE CASCADE,
  phone_id           uuid NOT NULL,
  source_template_id uuid,
  template_id        uuid NOT NULL REFERENCES phone_templates(id) ON DELETE CASCADE,
  PRIMARY KEY (execution_id, template_id)
);

DROP FUNCTION IF EXISTS exec_create(text, text, uuid, text, timestamptz, jsonb);

-- ============================================================
-- seek: contact active לפי contacts.number (+ provider לסינון ב-UI)
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
        'phone_status', p.status,
        'provider',     COALESCE(p.provider, 'baileys')
      ) ORDER BY p.number), '[]'::jsonb)
  )
  FROM contacts c
  JOIN phones p ON p.id = c.phone_id
  WHERE c.number = p_phone_number
    AND c.tag = 'active';
$$;

-- ============================================================
-- טלפוני מקור: טלפונים עם תרחישים active מקוריים (לא שכפולים)
-- ============================================================
CREATE OR REPLACE FUNCTION exec_list_sources()
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'phone_id',     p.id,
      'phone_number', p.number,
      'phone_status', p.status,
      'scenarios',    x.n
    ) ORDER BY p.number), '[]'::jsonb)
  FROM phones p
  JOIN (SELECT phone_id, count(*) AS n
          FROM scenarios
         WHERE status = 'active' AND source_scenario_id IS NULL
         GROUP BY phone_id) x ON x.phone_id = p.id;
$$;

-- ============================================================
-- תרחישים active מקוריים של טלפון(ים)
-- ============================================================
CREATE OR REPLACE FUNCTION exec_list_scenarios(p_phone_ids uuid[])
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id',           s.id,
      'name',         s.name,
      'phone_id',     s.phone_id,
      'phone_number', p.number,
      'event_type',   s.event_type,
      'description',  s.config->>'description'
    ) ORDER BY s.name), '[]'::jsonb)
  FROM scenarios s
  JOIN phones p ON p.id = s.phone_id
  WHERE s.phone_id = ANY(p_phone_ids)
    AND s.status = 'active'
    AND s.source_scenario_id IS NULL;
$$;

-- ============================================================
-- תבניות שתרחיש משתמש בהן (templateId / templateName בכל עומק של config)
-- ============================================================
CREATE OR REPLACE FUNCTION exec_source_templates(p_scenario_id uuid)
RETURNS SETOF phone_templates LANGUAGE sql STABLE AS $$
  WITH s AS (
    SELECT phone_id, config FROM scenarios WHERE id = p_scenario_id
  ),
  refs AS (
    SELECT o->>'templateId'   AS tid,
           o->>'templateName' AS tname,
           o->>'templateLang' AS tlang
    FROM s,
         jsonb_path_query(s.config,
           'lax $.** ? (@.templateId != null || @.templateName != null)') AS o
    WHERE jsonb_typeof(o) = 'object'
  )
  SELECT t.*
  FROM phone_templates t, s
  WHERE t.phone_id = s.phone_id
    AND EXISTS (SELECT 1 FROM refs r
                WHERE r.tid = t.id::text
                   OR (r.tname = t.name AND (r.tlang IS NULL OR r.tlang = t.lang)));
$$;

CREATE OR REPLACE FUNCTION exec_scenario_templates(p_scenario_id uuid)
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id',           t.id,
      'name',         t.name,
      'lang',         t.lang,
      'status',       t.status,
      'is_published', t.is_published
    ) ORDER BY t.name, t.lang), '[]'::jsonb)
  FROM exec_source_templates(p_scenario_id) t;
$$;

-- ============================================================
-- remap: מחליף templateId/templateName בכל עומק לפי מפה
--   מפתחות: "id:<uuid>" · "n:<name>|<lang>" · "n:<name>"
-- ============================================================
CREATE OR REPLACE FUNCTION exec_remap_templates(p_node jsonb, p_map jsonb)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  k     text;
  v     jsonb;
  v_out jsonb;
  m     jsonb;
BEGIN
  IF p_node IS NULL THEN
    RETURN NULL;
  END IF;

  IF jsonb_typeof(p_node) = 'array' THEN
    SELECT COALESCE(jsonb_agg(exec_remap_templates(e, p_map) ORDER BY ord), '[]'::jsonb)
      INTO v_out
      FROM jsonb_array_elements(p_node) WITH ORDINALITY AS a(e, ord);
    RETURN v_out;
  END IF;

  IF jsonb_typeof(p_node) <> 'object' THEN
    RETURN p_node;
  END IF;

  v_out := '{}'::jsonb;
  FOR k, v IN SELECT * FROM jsonb_each(p_node) LOOP
    v_out := v_out || jsonb_build_object(k, exec_remap_templates(v, p_map));
  END LOOP;

  m := NULL;
  IF v_out ? 'templateId' THEN
    m := p_map -> ('id:' || (v_out->>'templateId'));
  END IF;
  IF m IS NULL AND v_out ? 'templateName' THEN
    m := COALESCE(p_map -> ('n:' || (v_out->>'templateName') || '|' || COALESCE(v_out->>'templateLang', '')),
                  p_map -> ('n:' || (v_out->>'templateName')));
  END IF;

  IF m IS NOT NULL THEN
    IF v_out ? 'templateId' OR NOT (v_out ? 'templateName') THEN
      v_out := v_out || jsonb_build_object('templateId', m->>'id');
    END IF;
    IF v_out ? 'templateName' THEN
      v_out := v_out || jsonb_build_object('templateName', m->>'name');
    END IF;
  END IF;

  RETURN v_out;
END;
$$;

-- ============================================================
-- יצירה אטומית
--   p_targets: [{"phone_id":"uuid","contact_id":"uuid"}]
-- ============================================================
CREATE OR REPLACE FUNCTION exec_create(
  p_name                 text,
  p_contact_phone_number text,
  p_source_scenario_id   uuid,
  p_template_prefix      text,
  p_targets              jsonb
)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_exec_id   uuid;
  v_src       scenarios%ROWTYPE;
  v_tpl       phone_templates%ROWTYPE;
  v_t         jsonb;
  v_phone     uuid;
  v_contact   uuid;
  v_provider  text;
  v_new_name  text;
  v_new_tpl   uuid;
  v_new_scen  uuid;
  v_sched     uuid;
  v_map       jsonb;
  v_val       jsonb;
  v_links     jsonb := '[]'::jsonb;
  v_tpl_count int   := 0;
  v_name      text  := btrim(COALESCE(p_name, ''));
  v_job       text;
BEGIN
  IF v_name = '' THEN
    RAISE EXCEPTION 'name is required';
  END IF;
  IF p_template_prefix IS NULL OR p_template_prefix !~ '^[a-z][a-z0-9_]{0,39}$' THEN
    RAISE EXCEPTION 'invalid template prefix: % (a-z, 0-9, _ ; starts with a letter)', p_template_prefix;
  END IF;
  IF p_targets IS NULL OR jsonb_typeof(p_targets) <> 'array' OR jsonb_array_length(p_targets) = 0 THEN
    RAISE EXCEPTION 'targets is required';
  END IF;

  SELECT * INTO v_src FROM scenarios WHERE id = p_source_scenario_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'source scenario % not found', p_source_scenario_id;
  END IF;

  v_job := v_name || ' JOB';

  INSERT INTO executions (name, contact_phone_number, contact_name,
                          source_scenario_id, run_mode, status, template_prefix)
  VALUES (v_name, p_contact_phone_number,
          (SELECT name FROM contacts
            WHERE number = p_contact_phone_number AND tag = 'active' LIMIT 1),
          p_source_scenario_id, 'manual', 'stopped', p_template_prefix)
  RETURNING id INTO v_exec_id;

  FOR v_t IN SELECT * FROM jsonb_array_elements(p_targets)
  LOOP
    v_phone   := (v_t->>'phone_id')::uuid;
    v_contact := (v_t->>'contact_id')::uuid;

    SELECT COALESCE(provider, 'baileys') INTO v_provider FROM phones WHERE id = v_phone;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'phone % not found', v_phone;
    END IF;
    IF v_provider <> 'baileys' THEN
      RAISE EXCEPTION 'phone % is % — only baileys targets are supported', v_phone, v_provider;
    END IF;

    PERFORM 1 FROM contacts
     WHERE id = v_contact AND phone_id = v_phone AND tag = 'active';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'contact % is not active on phone %', v_contact, v_phone;
    END IF;

    -- ── תבניות: שכפול לטלפון היעד, approved + published ──
    v_map := '{}'::jsonb;
    FOR v_tpl IN SELECT * FROM exec_source_templates(p_source_scenario_id)
    LOOP
      v_new_name := p_template_prefix || '_' || v_tpl.name;

      PERFORM 1 FROM phone_templates
       WHERE phone_id = v_phone AND name = v_new_name AND lang = v_tpl.lang;
      IF FOUND THEN
        RAISE EXCEPTION 'template %/% already exists on phone % — choose another prefix',
          v_new_name, v_tpl.lang, v_phone;
      END IF;

      v_new_tpl := gen_random_uuid();

      INSERT INTO phone_templates
      SELECT * FROM jsonb_populate_record(
        NULL::phone_templates,
        to_jsonb(v_tpl) || jsonb_build_object(
          'id',                   v_new_tpl,
          'phone_id',             v_phone,
          'name',                 v_new_name,
          'status',               'approved',
          'is_published',         true,
          'rejected_reason',      NULL,
          'provider_template_id', NULL,
          'created_at',           now(),
          'updated_at',           now()));

      INSERT INTO execution_templates (execution_id, phone_id, source_template_id, template_id)
      VALUES (v_exec_id, v_phone, v_tpl.id, v_new_tpl);

      v_val := jsonb_build_object('id', v_new_tpl, 'name', v_new_name);
      v_map := v_map
            || jsonb_build_object('id:' || v_tpl.id::text, v_val)
            || jsonb_build_object('n:' || v_tpl.name || '|' || COALESCE(v_tpl.lang, ''), v_val);
      IF NOT (v_map ? ('n:' || v_tpl.name)) THEN
        v_map := v_map || jsonb_build_object('n:' || v_tpl.name, v_val);
      END IF;

      v_tpl_count := v_tpl_count + 1;
    END LOOP;

    -- ── תרחיש: "{run} {scenario}" עם הפניות לתבניות החדשות ──
    INSERT INTO scenarios (phone_id, contact_id, name, status, config,
                           event_type, priority,
                           estimated_duration_minutes, inter_leaf_response_time,
                           source_scenario_id)
    VALUES (v_phone,
            v_contact,
            v_name || ' ' || v_src.name,
            'active',
            exec_remap_templates(v_src.config, v_map),
            'scheduler',
            v_src.priority,
            v_src.estimated_duration_minutes,
            v_src.inter_leaf_response_time,
            p_source_scenario_id)
    RETURNING id INTO v_new_scen;

    -- ── תזמון manual: "{run} JOB", לא יורה עד exec_run ──
    INSERT INTO schedules (phone_id, contact_id, scenario_id, schedule_name,
                           schedule_type, status, run_at, next_run)
    VALUES (v_phone, v_contact, v_new_scen, v_job,
            'manual', 'active', NULL, NULL)
    RETURNING id INTO v_sched;

    INSERT INTO execution_links (execution_id, phone_id, contact_id,
                                 scenario_id, schedule_id)
    VALUES (v_exec_id, v_phone, v_contact, v_new_scen, v_sched);

    v_links := v_links || jsonb_build_object(
      'phone_id',    v_phone,
      'scenario_id', v_new_scen,
      'schedule_id', v_sched);
  END LOOP;

  RETURN jsonb_build_object(
    'execution_id', v_exec_id,
    'job_name',     v_job,
    'templates',    v_tpl_count,
    'links',        v_links);
END;
$$;

-- ============================================================
-- רשימה מועשרת
-- ============================================================
CREATE OR REPLACE FUNCTION exec_list()
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(row ORDER BY row->>'created_at' DESC), '[]'::jsonb)
  FROM (
    SELECT jsonb_build_object(
      'id',                   e.id,
      'name',                 e.name,
      'job_name',             e.name || ' JOB',
      'template_prefix',      e.template_prefix,
      'contact_phone_number', e.contact_phone_number,
      'contact_name',         e.contact_name,
      'source_scenario_id',   e.source_scenario_id,
      'source_scenario_name', ss.name,
      'source_phone_number',  sp.number,
      'run_mode',             e.run_mode,
      'status',               e.status,
      'created_at',           e.created_at,
      'phones', (SELECT COALESCE(jsonb_agg(p.number ORDER BY p.number), '[]'::jsonb)
                   FROM execution_links l
                   JOIN phones p ON p.id = l.phone_id
                  WHERE l.execution_id = e.id),
      'templates', (SELECT count(*) FROM execution_templates t
                     WHERE t.execution_id = e.id),
      'pending', (SELECT count(*) FROM execution_links l
                    JOIN schedules s ON s.id = l.schedule_id
                   WHERE l.execution_id = e.id
                     AND (s.status = 'firing' OR s.next_run IS NOT NULL)),
      'last_run', (SELECT max(s.last_run) FROM execution_links l
                     JOIN schedules s ON s.id = l.schedule_id
                    WHERE l.execution_id = e.id)
    ) AS row
    FROM executions e
    LEFT JOIN scenarios ss ON ss.id = e.source_scenario_id
    LEFT JOIN phones    sp ON sp.id = ss.phone_id
  ) t;
$$;

-- ============================================================
-- links של ביצוע
-- ============================================================
CREATE OR REPLACE FUNCTION exec_links(p_execution_id uuid)
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'phone_id',    l.phone_id,
      'contact_id',  l.contact_id,
      'scenario_id', l.scenario_id,
      'schedule_id', l.schedule_id)), '[]'::jsonb)
  FROM execution_links l
  WHERE l.execution_id = p_execution_id;
$$;

-- ============================================================
-- run: next_run=now() לכל התזמונים — ה-Scheduler מרים במקביל
-- ============================================================
CREATE OR REPLACE FUNCTION exec_run(p_execution_id uuid)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_n    int;
  v_busy int;
BEGIN
  PERFORM 1 FROM executions WHERE id = p_execution_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('ok', false, 'error', 'not_found');
  END IF;

  SELECT count(*) INTO v_busy
    FROM execution_links l
    JOIN schedules s ON s.id = l.schedule_id
   WHERE l.execution_id = p_execution_id
     AND s.status = 'firing';
  IF v_busy > 0 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'firing', 'firing', v_busy);
  END IF;

  UPDATE schedules s
     SET status     = 'active',
         next_run   = now(),
         updated_at = now()
    FROM execution_links l
   WHERE l.execution_id = p_execution_id
     AND s.id = l.schedule_id;
  GET DIAGNOSTICS v_n = ROW_COUNT;

  IF v_n = 0 THEN
    RETURN jsonb_build_object('ok', false, 'error', 'no_schedules');
  END IF;

  UPDATE executions SET status = 'running' WHERE id = p_execution_id;
  RETURN jsonb_build_object('ok', true, 'scheduled', v_n);
END;
$$;

-- ============================================================
-- stop: עוצר תזמונים שעוד לא נורו
-- ============================================================
CREATE OR REPLACE FUNCTION exec_stop(p_execution_id uuid)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_n int;
BEGIN
  UPDATE schedules s
     SET status     = 'paused',
         next_run   = NULL,
         updated_at = now()
    FROM execution_links l
   WHERE l.execution_id = p_execution_id
     AND s.id = l.schedule_id
     AND s.status <> 'firing';
  GET DIAGNOSTICS v_n = ROW_COUNT;

  UPDATE executions SET status = 'stopped' WHERE id = p_execution_id;
  RETURN jsonb_build_object('ok', true, 'paused', v_n);
END;
$$;

-- ============================================================
-- delete: ביצוע → תזמונים → תרחישים → תבניות
--   מה שמוחזק ב-FK (calls וכו') נשאר ומדווח ב-kept
-- ============================================================
CREATE OR REPLACE FUNCTION exec_delete(p_execution_id uuid)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_sched uuid[];
  v_scen  uuid[];
  v_tpl   uuid[];
  v_id    uuid;
  v_kept  jsonb := '[]'::jsonb;
BEGIN
  IF EXISTS (SELECT 1 FROM execution_links l
               JOIN schedules s ON s.id = l.schedule_id
              WHERE l.execution_id = p_execution_id AND s.status = 'firing') THEN
    RETURN jsonb_build_object('ok', false, 'error', 'firing');
  END IF;

  SELECT array_agg(schedule_id) FILTER (WHERE schedule_id IS NOT NULL),
         array_agg(scenario_id) FILTER (WHERE scenario_id IS NOT NULL)
    INTO v_sched, v_scen
    FROM execution_links WHERE execution_id = p_execution_id;

  SELECT array_agg(template_id) INTO v_tpl
    FROM execution_templates WHERE execution_id = p_execution_id;

  DELETE FROM executions WHERE id = p_execution_id;   -- links + templates ב-CASCADE
  IF NOT FOUND THEN
    RETURN jsonb_build_object('ok', false, 'error', 'not_found');
  END IF;

  FOREACH v_id IN ARRAY COALESCE(v_sched, '{}'::uuid[]) LOOP
    BEGIN
      DELETE FROM schedules WHERE id = v_id;
    EXCEPTION WHEN foreign_key_violation THEN
      UPDATE schedules SET status = 'paused', next_run = NULL WHERE id = v_id;
      v_kept := v_kept || jsonb_build_object('schedule', v_id);
    END;
  END LOOP;

  FOREACH v_id IN ARRAY COALESCE(v_scen, '{}'::uuid[]) LOOP
    BEGIN
      DELETE FROM scenarios WHERE id = v_id;
    EXCEPTION WHEN foreign_key_violation THEN
      v_kept := v_kept || jsonb_build_object('scenario', v_id);
    END;
  END LOOP;

  FOREACH v_id IN ARRAY COALESCE(v_tpl, '{}'::uuid[]) LOOP
    BEGIN
      DELETE FROM phone_templates WHERE id = v_id;
    EXCEPTION WHEN foreign_key_violation THEN
      v_kept := v_kept || jsonb_build_object('template', v_id);
    END;
  END LOOP;

  RETURN jsonb_build_object('ok', true, 'kept', v_kept);
END;
$$;
