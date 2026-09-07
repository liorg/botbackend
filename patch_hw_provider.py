from pathlib import Path

P = Path("routers/template_manager.py")
s = P.read_text(encoding="utf-8")

if "hello_world skipped" in s:
    raise SystemExit("already patched")

ANCHOR = '''def ensure_hello_world(db: Client, phone_id: str) -> Optional[dict]:
    existing = ('''

NEW = '''def ensure_hello_world(db: Client, phone_id: str) -> Optional[dict]:
    phone = _phone_row(db, phone_id)
    if (phone.get("provider") or "baileys") != "baileys":
        logger.info(f"[TPL] hello_world skipped — provider={phone.get('provider')} phone={phone_id}")
        return None

    existing = ('''

if s.count(ANCHOR) != 1:
    raise SystemExit(f"anchor count={s.count(ANCHOR)} — abort")

P.write_text(s.replace(ANCHOR, NEW), encoding="utf-8")
print("patched")
