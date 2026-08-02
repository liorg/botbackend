-- ============================================================================
-- messages_page — RPC אחד לשלושה מצבים:
--   1. עמוד ראשון              → בלי cursors
--   2. עמוד ישן יותר (scroll)  → p_before_sent_at + p_before_id
--   3. polling הודעות חדשות     → p_after_sent_at
-- עמודות תואמות לקוד הקיים: id, contact_id, phone_id, sender, content,
--                            sent_at, direction, media_url
-- כולל fallback כמו ב-messages.py: אם אין הודעות עם phone_id → phone_id IS NULL
-- ============================================================================

create or replace function messages_page(
  p_contact_id     uuid,
  p_phone_id       uuid        default null,
  p_limit          int         default 30,
  p_before_sent_at timestamptz default null,
  p_before_id      uuid        default null,
  p_after_sent_at  timestamptz default null
)
returns jsonb
language plpgsql
stable
as $$
declare
  v_null_phone boolean := false;
  v_arr        jsonb;
  v_has        boolean := false;
begin
  -- fallback זהה לקוד הקיים
  if p_phone_id is not null and not exists (
    select 1 from messages
    where contact_id = p_contact_id and phone_id = p_phone_id
  ) then
    v_null_phone := true;
  end if;

  -- ── מצב 3: polling — רק הודעות חדשות מ-cursor והלאה ──────────────────
  if p_after_sent_at is not null then
    select coalesce(jsonb_agg(to_jsonb(m) order by m.sent_at, m.id), '[]'::jsonb)
    into v_arr
    from (
      select id, contact_id, phone_id, sender, content, sent_at, direction, media_url
      from messages
      where contact_id = p_contact_id
        and ( p_phone_id is null
              or (not v_null_phone and phone_id = p_phone_id)
              or (v_null_phone and phone_id is null) )
        and sent_at > p_after_sent_at
      order by sent_at, id
      limit p_limit
    ) m;

    return jsonb_build_object('messages', v_arr, 'has_more', false, 'next_cursor', null);
  end if;

  -- ── מצב 1+2: עמוד רגיל, keyset אחורה (limit+1 כדי לדעת אם יש עוד) ───
  select coalesce(jsonb_agg(to_jsonb(p) order by p.sent_at desc, p.id desc), '[]'::jsonb)
  into v_arr
  from (
    select id, contact_id, phone_id, sender, content, sent_at, direction, media_url
    from messages
    where contact_id = p_contact_id
      and ( p_phone_id is null
            or (not v_null_phone and phone_id = p_phone_id)
            or (v_null_phone and phone_id is null) )
      and ( p_before_sent_at is null
            or (sent_at, id) < (p_before_sent_at, p_before_id) )
    order by sent_at desc, id desc
    limit p_limit + 1
  ) p;

  v_has := jsonb_array_length(v_arr) > p_limit;

  -- חיתוך ל-limit (הסרת האיבר העודף)
  if v_has then
    select jsonb_agg(elem order by ord)
    into v_arr
    from jsonb_array_elements(v_arr) with ordinality t(elem, ord)
    where ord <= p_limit;
  end if;

  -- היפוך לסדר עולה (ישן → חדש) — מוכן לרינדור ישיר
  select coalesce(jsonb_agg(elem order by ord desc), '[]'::jsonb)
  into v_arr
  from jsonb_array_elements(v_arr) with ordinality t(elem, ord);

  return jsonb_build_object(
    'messages', v_arr,
    'has_more', v_has,
    'next_cursor', case
      when v_has and jsonb_array_length(v_arr) > 0 then jsonb_build_object(
        'sent_at', v_arr->0->>'sent_at',   -- ההודעה הישנה ביותר בעמוד
        'id',      v_arr->0->>'id'
      )
      else null
    end
  );
end;
$$;

-- אינדקס תומך (אם לא קיים):
-- create index if not exists idx_messages_contact_sent
--   on messages (contact_id, sent_at desc, id desc);
