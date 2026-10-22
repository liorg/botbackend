"""Verify the backend after a refactor, before commit.

  1. compile   every .py compiles
  2. import    `main` imports and exposes a FastAPI `app`
  3. routes    no route lost or changed vs. a snapshot taken before the change
  4. deps      check_deps: ERROR 0, FIX 0 (MANUAL is reported, not blocking)

Usage (from the backend root):
  python scripts/verify_backend.py --snapshot   before the change: save routes
  python scripts/verify_backend.py              after the change: verify

Exit code 0 = safe to commit.
"""

import importlib
import inspect
import json
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
SNAPSHOT = ROOT / ".routes_snapshot.json"
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git"}
CHECK_DEPS = Path(__file__).with_name("check_deps.py")

failed = False


def ok(msg):
    print(f"  ✓ {msg}")


def bad(msg):
    global failed
    failed = True
    print(f"  ✗ {msg}")


def step_compile():
    print("1. compile")
    errors = 0
    for path in sorted(ROOT.rglob("*.py")):
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            errors += 1
            bad(f"{path.relative_to(ROOT)}: {e.msg.strip().splitlines()[-1]}")
    if not errors:
        ok("all files compile")


def load_app():
    sys.path.insert(0, str(ROOT))
    try:
        main = importlib.import_module("main")
    except Exception as e:
        bad(f"import main: {type(e).__name__}: {e}")
        return None
    app = getattr(main, "app", None)
    if app is None:
        bad("main has no `app`")
        return None
    return app


def _walk(routes, prefix=""):
    """Yield (path, methods, endpoint) for every route.

    Older FastAPI flattens include_router into APIRoute objects with the
    full path. Newer FastAPI keeps an _IncludedRouter wrapper; recurse into
    its original_router and add its prefix.
    """
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            ctx = getattr(r, "include_context", None)
            yield from _walk(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            continue
        path = getattr(r, "path", None)
        endpoint = getattr(r, "endpoint", None)
        if path is None or endpoint is None:
            continue
        yield prefix + path, sorted(getattr(r, "methods", None) or []), endpoint


def routes_of(app) -> dict:
    out = {}
    for path, methods, endpoint in _walk(app.routes):
        for m in methods or ["*"]:
            out[f"{m} {path}"] = {
                "endpoint": f"{endpoint.__module__}.{endpoint.__name__}",
                "async": inspect.iscoroutinefunction(endpoint),
            }
    return out


def step_import_and_routes(snapshot_only: bool):
    print("2. import")
    app = load_app()
    if app is None:
        return
    current = routes_of(app)
    ok(f"main.app loaded, {len(current)} routes")

    if snapshot_only:
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
        ok(f"snapshot saved: {SNAPSHOT.name}")
        return

    print("3. routes")
    if not SNAPSHOT.exists():
        bad(f"no {SNAPSHOT.name} — run with --snapshot before the change")
        return

    before = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    lost = sorted(set(before) - set(current))
    added = sorted(set(current) - set(before))
    moved = sorted(k for k in set(before) & set(current)
                   if before[k]["endpoint"] != current[k]["endpoint"])
    to_sync = sorted(k for k in set(before) & set(current)
                     if before[k]["async"] and not current[k]["async"])
    to_async = sorted(k for k in set(before) & set(current)
                      if not before[k]["async"] and current[k]["async"])

    for k in lost:
        bad(f"route lost: {k} ({before[k]['endpoint']})")
    for k in moved:
        bad(f"route handler changed: {k}: {before[k]['endpoint']} -> {current[k]['endpoint']}")
    for k in added:
        print(f"  + route added: {k}")
    for k in to_async:
        print(f"  ! def -> async def: {k}")

    if not lost and not moved:
        ok(f"all {len(before)} routes still there; {len(to_sync)} async -> def")


def step_deps():
    print("4. deps")
    if not CHECK_DEPS.exists():
        bad(f"{CHECK_DEPS} not found")
        return
    res = subprocess.run([sys.executable, str(CHECK_DEPS), "."],
                         capture_output=True, text=True, cwd=ROOT)
    counts = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in {"ERROR", "FIX", "MANUAL"}:
            counts[parts[0]] = int(parts[1])
    if not counts:
        bad("could not read check_deps output")
        print(res.stdout + res.stderr)
        return
    for kind in ("ERROR", "FIX"):
        if counts.get(kind):
            bad(f"{kind} {counts[kind]} — run check_deps.py to see them")
    if not counts.get("ERROR") and not counts.get("FIX"):
        ok("ERROR 0, FIX 0")
    if counts.get("MANUAL"):
        print(f"  · MANUAL {counts['MANUAL']} (not blocking)")


def main():
    snapshot_only = "--snapshot" in sys.argv

    if snapshot_only:
        step_import_and_routes(snapshot_only=True)
    else:
        step_compile()
        step_import_and_routes(snapshot_only=False)
        step_deps()

    print()
    print("FAILED" if failed else ("snapshot ready" if snapshot_only else "OK — safe to commit"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()