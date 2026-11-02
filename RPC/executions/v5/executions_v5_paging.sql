-- ============================================================
-- executions — migration v5: paging ל-exec_list
--   גודל עמוד מ-bot_config 'executions.paging' (ברירת מחדל 10, 1–100)
--   מחזיר { items, total, page, page_size, total_pages } — כמו scenarios
--   דורש v4 (v_executions). בטוח להרצה חוזרת.
-- ============================================================

DROP FUNCTION IF EXISTS exec_list();
DROP FUNCTION IF EXISTS exec_list(int);

INSERT INTO bot_config (key, value)
SELECT 'executions.paging', '10'
 WHERE NOT EXISTS (SELECT 1 FROM bot_config WHERE key = 'executions.paging');

CREATE OR REPLACE FUNCTION exec_list(p_page int DEFAULT 1)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_size  int;
  v_page  int := GREATEST(COALESCE(p_page, 1), 1);
  v_total int;
  v_pages int;
  v_items jsonb;
BEGIN
  SELECT NULLIF(regexp_replace(value::text, '\D', '', 'g'), '')::int
    INTO v_size
    FROM bot_config
   WHERE key = 'executions.paging'
   LIMIT 1;
  v_size := LEAST(GREATEST(COALESCE(v_size, 10), 1), 100);

  SELECT count(*) INTO v_total FROM v_executions;
  v_pages := GREATEST(CEIL(v_total::numeric / v_size)::int, 1);
  v_page  := LEAST(v_page, v_pages);   -- עמוד שהתרוקן (מחיקה) → העמוד האחרון

  SELECT COALESCE(jsonb_agg(to_jsonb(v) ORDER BY v.created_at DESC, v.id), '[]'::jsonb)
    INTO v_items
    FROM (
      SELECT *
        FROM v_executions
       ORDER BY created_at DESC, id
       LIMIT v_size OFFSET (v_page - 1) * v_size
    ) v;

  RETURN jsonb_build_object(
    'items',       v_items,
    'total',       v_total,
    'page',        v_page,
    'page_size',   v_size,
    'total_pages', v_pages);
END;
$$;
