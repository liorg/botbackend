
# botbackend
=======
# ScenarioBot — FastAPI Backend

## התקנה

```bash
pip install -r requirements.txt
cp .env.example .env
# מלא את הערכים ב-.env
```

## הרצה

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
# פיתוח עם reload:
uvicorn main:app --reload --port 8000
```

## מבנה

```
├── main.py              # נקודת כניסה + CORS
├── dependencies.py      # JWT auth + Supabase client
├── requirements.txt
├── .env.example
├── routers/
│   ├── auth.py          # POST /auth/google
│   ├── phones.py        # CRUD /phones
│   ├── contacts.py      # CRUD /contacts
│   ├── scenarios.py     # CRUD /scenarios
│   ├── schedules.py     # CRUD /schedules
│   └── calls.py         # CRUD /calls + /messages
└── sql/
    ├── 01_users.sql
    ├── 02_phones.sql
    ├── 03_contacts.sql
    ├── 04_scenarios.sql
    ├── 05_schedules.sql
    ├── 06_calls.sql
    ├── 07_messages.sql
    └── 08_rls.sql
```

## Supabase SQL

הרץ את קבצי ה-SQL לפי הסדר (01 → 08) ב-Supabase SQL Editor.

## API Docs

לאחר הרצה: http://localhost:8000/docs
