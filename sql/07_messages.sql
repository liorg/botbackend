-- ============================================================
-- 07_messages.sql
-- ============================================================

create table if not exists messages (
  id      uuid default gen_random_uuid() primary key,
  call_id uuid references calls(id) on delete cascade,

  -- מי שלח: bot = ה-bot, test = טלפון הבדיקה (האדם)
  sender  text not null check (sender in ('bot', 'test')),

  -- כל התוכן ב-jsonb אחד לפי סוג:
  --
  -- טקסט רגיל:
  -- { "type": "text", "text": "שלום!" }
  --
  -- תפריט (menu):
  -- { "type": "menu",
  --   "header": "בחר נושא",
  --   "body": "אנא בחר מהרשימה:",
  --   "footer": "ScenarioBot",
  --   "button_text": "פתח תפריט",
  --   "sections": [
  --     { "title": "שירותים",
  --       "rows": [
  --         { "id": "s1", "title": "תמיכה", "description": "פתח פנייה" },
  --         { "id": "s2", "title": "מכירות", "description": "דבר עם נציג" }
  --       ]
  --     }
  --   ]
  -- }
  --
  -- כפתורים (buttons):
  -- { "type": "buttons",
  --   "body": "האם אתה מעוניין?",
  --   "footer": "בחר תשובה",
  --   "buttons": [
  --     { "id": "yes",   "title": "כן" },
  --     { "id": "no",    "title": "לא" },
  --     { "id": "later", "title": "אחר כך" }
  --   ]
  -- }
  --
  -- בחירת כפתור (מ-test):
  -- { "type": "button_reply",
  --   "selected_id": "yes",
  --   "selected_title": "כן" }
  --
  -- בחירת תפריט (מ-test):
  -- { "type": "menu_reply",
  --   "selected_id": "s1",
  --   "selected_title": "תמיכה" }

  content jsonb not null,

  status  text default 'sent',   -- sent | delivered | read | failed
  sent_at timestamp default now()
);

create index if not exists messages_call_id_idx      on messages(call_id);
create index if not exists messages_call_sent_at_idx on messages(call_id, sent_at);
