-- ============================================================
-- executions — ניקוי כשתרחיש נמחק (גרסה 2)
--
--   הביצוע עצמו לא נמחק. יורד רק הטלפון שבו היה התרחיש:
--
--   exec_cleanup_scenario(scenario_id)
--     שכפול (link)  → מאותו טלפון בלבד: תזמון "{run} JOB", התבניות "{prefix}_*"
--                     שנוצרו לו, וה-link. שאר הטלפונים בביצוע ממשיכים כרגיל.
--                     את התרחיש עצמו מוחק מי שקרא.
--     מקור          → לא נוגעים בשכפולים (כל אחד עצמאי, עם תבניות משלו);
--                     רק מנתקים את ההפניה: executions.source_scenario_id
--                     ו-scenarios.source_scenario_id ← NULL.
--
--   trg_scenarios_exec_cleanup — BEFORE DELETE על scenarios:
--     כל מחיקה של תרחיש, מכל מסלול (API / SQL / Supabase), מפעילה את הניקוי.
--
--   שורה שמוחזקת ב-FK לא נמחקת — נרשמת ב-kept (תזמון → paused).
-- ============================================================

DROP FUNCTION IF EXISTS exec_drop_link(uuid, uuid, boolean);

-- ── פירוק הטלפון של link אחד ─────────────────────────────────
CREATE OR REPLACE FUNCTION exec_drop_link(
  p_execution_id uuid,
  p_scenario_id  uuid
)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_phone uuid;
  v_sched uuid;
  v_tpl   uuid;
  v_kept  jsonb := '[]'::jsonb;
BEGIN
  DELETE FROM execution_links
   WHERE execution_id = p_execution_id
     AND scenario_id  = p_scenario_id
  RETURNING phone_id, schedule_id INTO v_phone, v_sched;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('scenario_id', p_scenario_id, 'dropped', false);
  END IF;

  -- תזמון של הטלפון
  IF v_sched IS NOT NULL THEN
    BEGIN
      DELETE FROM schedules WHERE id = v_sched;
    EXCEPTION WHEN foreign_key_violation THEN
      UPDATE schedules SET status = 'paused', next_run = NULL WHERE id = v_sched;
      v_kept := v_kept || jsonb_build_object('schedule', v_sched);
    END;
  END IF;

  -- תבניות שנוצרו לטלפון הזה בביצוע הזה (ייחודיות פר טלפון)
  FOR v_tpl IN
    DELETE FROM execution_templates
     WHERE execution_id = p_execution_id
       AND phone_id     = v_phone
    RETURNING template_id
  LOOP
    BEGIN
      DELETE FROM phone_templates WHERE id = v_tpl;
    EXCEPTION WHEN foreign_key_violation THEN
      v_kept := v_kept || jsonb_build_object('template', v_tpl);
    END;
  END LOOP;

  RETURN jsonb_build_object(
    'execution_id', p_execution_id,
    'scenario_id',  p_scenario_id,
    'phone_id',     v_phone,
    'dropped',      true,
    'kept',         v_kept);
END;
$$;

-- ── RPC: ניקוי לפי תרחיש שנמחק ────────────────────────────────
CREATE OR REPLACE FUNCTION exec_cleanup_scenario(p_scenario_id uuid)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  r     record;
  v_out jsonb := '[]'::jsonb;
BEGIN
  -- שכפול → רק הטלפון שלו יורד מהביצוע
  FOR r IN
    SELECT execution_id, scenario_id
      FROM execution_links
     WHERE scenario_id = p_scenario_id
  LOOP
    v_out := v_out || exec_drop_link(r.execution_id, r.scenario_id);
  END LOOP;

  -- מקור → ניתוק בלבד, השכפולים ממשיכים לרוץ
  UPDATE executions SET source_scenario_id = NULL
   WHERE source_scenario_id = p_scenario_id;
  UPDATE scenarios  SET source_scenario_id = NULL
   WHERE source_scenario_id = p_scenario_id;

  RETURN jsonb_build_object('ok', true, 'links', v_out);
END;
$$;

-- ── trigger: כל מחיקת תרחיש מנקה את הטלפון שלו מהביצוע ─────────
CREATE OR REPLACE FUNCTION trg_scenarios_exec_cleanup()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM exec_cleanup_scenario(OLD.id);
  RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_scenarios_exec_cleanup ON scenarios;
CREATE TRIGGER trg_scenarios_exec_cleanup
  BEFORE DELETE ON scenarios
  FOR EACH ROW EXECUTE FUNCTION trg_scenarios_exec_cleanup();
