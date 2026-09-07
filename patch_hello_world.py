# patch_hello_world.py
from pathlib import Path

P1 = Path("routers/template_manager.py")
P2 = Path("routers/phones.py")

BLOCK = '''

# ══════════════════════════════════════════════════════════════════════════
# Seed — תבנית hello_world כמו ב-WABA, נוצרת אוטומטית ב-provision
# ══════════════════════════════════════════════════════════════════════════

HELLO_WORLD_NAME = "hello_world"
HELLO_WORLD_LANG = "en_US"

HELLO_WORLD_CONTENT: dict = {
    "header": {"format": "text", "text": "Hello World"},
    "body": {
        "text": (
            "Welcome and congratulations!! This message demonstrates your ability "
            "to send a WhatsApp message notification from the Cloud API, hosted by "
            "Meta. Thank you for taking the time to test with us."
        )
    },
    "footer": {"text": "WhatsApp Business Platform sample message"},
    "buttons": [],
}

HELLO_WORLD_EXAMPLES: dict = {"header": [], "body": [], "header_media_url": None}


def ensure_hello_world(db: Client, phone_id: str) -> Optional[dict]:
    existing = (
        db.table("phone_templates")
        .select(_SELECT)
        .eq("phone_id", phone_id)
        .eq("name", HELLO_WORLD_NAME)
        .eq("lang", HELLO_WORLD_LANG)
        .limit(1)
        .execute()
    )
    if existing.data:
        return _expand(existing.data[0])

    content = _norm_content(HELLO_WORLD_CONTENT)
    examples = _norm_examples(HELLO_WORLD_EXAMPLES)

    issues = _validate(HELLO_WORLD_NAME, HELLO_WORLD_LANG, content, examples)
    if issues:
        logger.error(f"[TPL] hello_world seed invalid: {issues}")
        return None

    payload = {
        "id": str(uuid.uuid4()),
        "phone_id": phone_id,
        "name": HELLO_WORLD_NAME,
        "category": "UTILITY",
        "lang": HELLO_WORLD_LANG,
        "content": content,
        "examples": examples,
        "status": "approved",
        "is_published": True,
        "param_count": _count_params(content),
        "provider_template_id": None,
        "rejected_reason": None,
        "created_at": _now(),
        "updated_at": _now(),
    }

    result = db.table("phone_templates").insert(payload).execute()
    if not result.data:
        logger.error(f"[TPL] hello_world seed failed phone={phone_id}")
        return None

    logger.info(f"[TPL] seeded hello_world/en_US phone={phone_id}")
    return _expand(result.data[0])
'''

s1 = P1.read_text(encoding="utf-8")
if "def ensure_hello_world" not in s1:
    P1.write_text(s1.rstrip() + "\n" + BLOCK, encoding="utf-8")
    print("template_manager.py patched")
else:
    print("template_manager.py already patched")

ANCHOR = '        phone_id = data.get("phoneId") or (phone["id"] if phone else None)\n'
ADD = ANCHOR + '''
        if phone_id:
            try:
                from routers.template_manager import ensure_hello_world
                ensure_hello_world(db, phone_id)
            except Exception as e:
                logger.warning(f"[PROVISION] hello_world seed failed {phone_id}: {e}")
'''

s2 = P2.read_text(encoding="utf-8")
if "ensure_hello_world" in s2:
    print("phones.py already patched")
elif s2.count(ANCHOR) != 1:
    raise SystemExit(f"anchor count={s2.count(ANCHOR)} — abort")
else:
    P2.write_text(s2.replace(ANCHOR, ADD), encoding="utf-8")
    print("phones.py patched")