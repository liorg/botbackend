-- ============================================================
-- executions — autocomplete אנשי קשר active של המשתמש (לפי phones.user_id)
--   קבוצה לפי contacts.number: שם, כמה טלפונים, אילו טלפונים
--   p_q: ספרות → חיפוש במספר · טקסט → חיפוש בשם · ריק → הכי מקושרים קודם
-- ============================================================

CREATE OR REPLACE FUNCTION exec_search_contacts(
  p_user_id uuid,
  p_q       text DEFAULT '',
  p_limit   int  DEFAULT 20
)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_q      text := btrim(COALESCE(p_q, ''));
  v_digits text;
  v_like   text;
  v_limit  int  := GREATEST(1, LEAST(COALESCE(p_limit, 20), 50));
  v_out    jsonb;
BEGIN
  v_digits := regexp_replace(v_q, '\D', '', 'g');
  v_like   := replace(replace(replace(v_q, '\', '\\'), '%', '\%'), '_', '\_');

  SELECT COALESCE(jsonb_agg(r ORDER BY ord), '[]'::jsonb)
    INTO v_out
    FROM (
      SELECT jsonb_build_object(
               'number',        c.number,
               'contact_name',  (array_agg(c.name ORDER BY c.name)
                                   FILTER (WHERE COALESCE(c.name, '') <> ''))[1],
               'phones',        count(DISTINCT c.phone_id),
               'phone_numbers', jsonb_agg(DISTINCT p.number)
             ) AS r,
             row_number() OVER (ORDER BY count(DISTINCT c.phone_id) DESC, c.number) AS ord
        FROM contacts c
        JOIN phones   p ON p.id = c.phone_id
       WHERE c.tag = 'active'
         AND p.user_id = p_user_id
         AND c.number ~ '^972[0-9]{8,10}$'
         AND (
               v_q = ''
            OR (v_digits <> '' AND c.number LIKE '%' || v_digits || '%')
            OR c.name ILIKE '%' || v_like || '%'
         )
       GROUP BY c.number
       ORDER BY count(DISTINCT c.phone_id) DESC, c.number
       LIMIT v_limit
    ) t;

  RETURN v_out;
END;
$$;
