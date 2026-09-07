"""חד-פעמי: seed hello_world/en_US לכל הטלפונים הקיימים."""
import sys
from dependencies import get_supabase
from routers.template_manager import (
    ensure_hello_world, HELLO_WORLD_NAME, HELLO_WORLD_LANG,
)

DRY = "--dry" in sys.argv

db = next(get_supabase()) if callable(getattr(get_supabase, "__call__", None)) and hasattr(get_supabase, "__wrapped__") else get_supabase()

phones = db.table("phones").select("id, number, provider").execute().data or []
existing = (
    db.table("phone_templates")
    .select("phone_id")
    .eq("name", HELLO_WORLD_NAME)
    .eq("lang", HELLO_WORLD_LANG)
    .execute()
).data or []
have = {r["phone_id"] for r in existing}

todo = [p for p in phones if p["id"] not in have]
print(f"phones={len(phones)} already={len(have)} todo={len(todo)}")

if DRY:
    for p in todo:
        print(f"  would seed {p['id']} ({p.get('number')})")
    raise SystemExit(0)

ok = fail = 0
for p in todo:
    try:
        res = ensure_hello_world(db, p["id"])
        if res:
            ok += 1
            print(f"  ✓ {p['id']} ({p.get('number')})")
        else:
            fail += 1
            print(f"  ✗ {p['id']} — returned None")
    except Exception as e:
        fail += 1
        print(f"  ✗ {p['id']} — {e}")

print(f"done: ok={ok} fail={fail}")
