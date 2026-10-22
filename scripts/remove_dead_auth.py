"""Remove the unused email/OAuth endpoints from routers/auth.py.

The frontend signs in, signs up and resets passwords directly against
Supabase or through routers/auth_email.py; these four are never called:

    POST /auth/google  /auth/login  /auth/signup  /auth/forgot-password

Removes each function with its decorators, then lists helpers, models and
imports in auth.py that only those functions used. Those are reported, not
deleted: check they are not imported elsewhere first.

Run from the backend root:  python scripts/remove_dead_auth.py
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

AUTH = Path("routers/auth.py")
DEAD = {"google_auth", "login", "signup", "forgot_password"}


def names_used(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def top_level_names(tree) -> set[str]:
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
    return out


def main():
    src = AUTH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    targets = [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in DEAD]

    found = {n.name for n in targets}
    missing = DEAD - found
    if missing:
        print(f"· not found (already removed?): {', '.join(sorted(missing))}")
    if not targets:
        return

    used_by_dead = set()
    drop = set()
    for fn in targets:
        used_by_dead |= names_used(fn)
        start = min([d.lineno for d in fn.decorator_list] + [fn.lineno])
        for i in range(start - 1, fn.end_lineno):
            drop.add(i)

    new = "".join(l for i, l in enumerate(lines) if i not in drop)
    new = re.sub(r"\n{4,}", "\n\n\n", new)

    new_tree = ast.parse(new)   # fails loudly if the cut broke the file
    AUTH.write_text(new, encoding="utf-8")
    print(f"✓ removed from {AUTH}: {', '.join(sorted(found))}")

    # what in auth.py did only the dead functions use?
    still_used = set()
    for n in new_tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body_uses = names_used(n) - {n.name}
            still_used |= body_uses
        else:
            still_used |= names_used(n)

    orphans = sorted((used_by_dead & top_level_names(new_tree)) - still_used)
    if not orphans:
        print("✓ no helpers left orphaned")
        return

    print("\nnow unused in auth.py (check, then delete by hand):")
    for name in orphans:
        res = subprocess.run(
            ["grep", "-rlw", "--include=*.py", name, "."],
            capture_output=True, text=True,
        )
        elsewhere = [f for f in res.stdout.split() if not f.endswith("routers/auth.py")
                     and "/scripts/" not in f]
        where = f"  ← also in: {', '.join(elsewhere)}" if elsewhere else ""
        print(f"  {name}{where}")


if __name__ == "__main__":
    sys.exit(main())