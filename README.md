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
sudo systemctl restart fastapi.service

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
python3 -m py_compile ./routers/contacts.py && echo OK || echo FAIL
python3 -m py_compile ./routers/messages.py && echo OK || echo FAIL

python3 -m py_compile ./dependencies.py && echo OK || echo FAIL
python3 -m py_compile ./routers/calls.py && echo OK || echo FAIL

python3 -m py_compile ./routers/proxy_media.py && echo OK || echo FAIL


# LOGGING

journalctl -u fastapi.service -n 50 --no-pager | grep -i "phones\|list\|uid"

journalctl -u fastapi.service -n 50 --no-pager | grep -A5 "calls/phone"

journalctl -u fastapi.service -n 50 --no-pager | grep -E "(calls|ERROR|error|500|422|phone)"

# Test Agent Alive

sudo systemctl status whatsapp-manager
sudo journalctl -u whatsapp-manager -n 50

# Git
git add .
git commit --m 'version 1.55'
git push