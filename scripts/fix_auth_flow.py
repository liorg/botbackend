"""Separate client for auth flows (sign-in / sign-up / reset).

Signing in on a supabase-py client switches its postgrest Authorization to
the user's token. auth.py signs in on _public_db() and then runs
_upsert_user_row / users insert on the same client, so those queries run as
the user, and if _public_db() is the shared service client, so does every
later service-role query in the process.

After this script:
  auth_db = new_auth_flow_client()   -> only the .auth.* call
  db      = _public_db()             -> every table / storage query (unchanged)

Run from the backend root:  python scripts/fix_auth_flow.py
"""

import re
import sys
from pathlib import Path

DEPS = Path("dependencies.py")
AUTH = Path("routers/auth.py")

NEW_FN = '''

def new_auth_flow_client() -> Client:
    """Fresh client per auth flow (sign-in, sign-up, OAuth, password reset).

    Never cached and never used for table queries: signing in switches the
    client's postgrest Authorization to the user's token, so a shared client
    would run every later service-role query as that user.
    """
    return create_client(
        _get_supabase_url(),
        _get_service_key(),
        options=ClientOptions(
            persist_session=False,
            auto_refresh_token=False,
        ),
    )
'''

# end of get_service_supabase (docstring close + return): unique, unlike a
# bare "return _service_client()" which get_supabase also contains
ANCHOR_AFTER = '''    """
    return _service_client()
'''

# (old, new) — each must match exactly once
AUTH_EDITS = [
    ("async def google_auth(request: GoogleTokenRequest):\n    db = _public_db()\n",
     "async def google_auth(request: GoogleTokenRequest):\n    db = _public_db()\n    auth_db = new_auth_flow_client()\n"),
    ("result = db.auth.sign_in_with_id_token(credentials)",
     "result = auth_db.auth.sign_in_with_id_token(credentials)"),

    ("async def login(request: LoginRequest):\n    db = _public_db()\n",
     "async def login(request: LoginRequest):\n    db = _public_db()\n    auth_db = new_auth_flow_client()\n"),
    ("result = db.auth.sign_in_with_password({",
     "result = auth_db.auth.sign_in_with_password({"),

    ("async def signup(request: SignupRequest):\n    db = _public_db()\n",
     "async def signup(request: SignupRequest):\n    db = _public_db()\n    auth_db = new_auth_flow_client()\n"),
    ("result = db.auth.sign_up({",
     "result = auth_db.auth.sign_up({"),

    ("async def forgot_password(request: ForgotPasswordRequest):\n    db = _public_db()\n",
     "async def forgot_password(request: ForgotPasswordRequest):\n    auth_db = new_auth_flow_client()\n"),
    ("        db.auth.reset_password_email(\n",
     "        auth_db.auth.reset_password_email(\n"),
]


def fail(msg):
    print(f"✗ {msg}")
    sys.exit(1)


def main():
    # 1. dependencies.py
    deps = DEPS.read_text(encoding="utf-8")
    if "def new_auth_flow_client" in deps:
        print("· dependencies.py: new_auth_flow_client already there")
    else:
        if deps.count(ANCHOR_AFTER) != 1:
            fail("dependencies.py: end of get_service_supabase not found exactly once")
        deps = deps.replace(ANCHOR_AFTER, ANCHOR_AFTER + NEW_FN, 1)
        DEPS.write_text(deps, encoding="utf-8")
        print("✓ dependencies.py: new_auth_flow_client added")

    # 2. routers/auth.py — validate all edits before writing any
    auth = AUTH.read_text(encoding="utf-8")
    if "new_auth_flow_client()" in auth:
        print("· routers/auth.py: already converted")
        return

    for old, _ in AUTH_EDITS:
        n = auth.count(old)
        if n != 1:
            fail(f"routers/auth.py: expected 1 match, found {n}:\n    {old!r}")

    for old, new in AUTH_EDITS:
        auth = auth.replace(old, new, 1)

    # import
    m = re.search(r"^from dependencies import ([^\n(]+)$", auth, re.M)
    if m:
        auth = auth.replace(m.group(0), m.group(0).rstrip() + ", new_auth_flow_client", 1)
    else:
        m = re.search(r"^from dependencies import \(", auth, re.M)
        if m:
            auth = auth.replace(m.group(0), m.group(0) + "\n    new_auth_flow_client,", 1)
        else:
            auth = "from dependencies import new_auth_flow_client\n" + auth

    AUTH.write_text(auth, encoding="utf-8")
    print("✓ routers/auth.py: 4 auth calls moved to auth_db")


if __name__ == "__main__":
    main()