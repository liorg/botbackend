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



## API Docs

לאחר הרצה: http://localhost:8000/d
ocs

## Compile
python3 -m py_compile main.py && echo OK || echo FAIL
python3 -m py_compile ./routers/auth.py && echo OK || echo FAIL
python3 -m py_compile ./routers/phones.py && echo OK || echo FAIL



## Test Agent Alive

sudo systemctl status whatsapp-manager
sudo journalctl -u whatsapp-manager -n 50