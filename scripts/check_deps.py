"""Find code that breaks the sync dependencies.py contract.

  ERROR   get_auth / get_current_user / get_supabase called directly.
          Breaks at runtime with the new dependencies.py.
  FIX     async def + Depends(get_supabase), no await inside.
          Safe to turn into def; --fix does it.
  MANUAL  async def + Depends(get_supabase) that awaits something and
          still touches db on the event loop. Cannot become def: wrap
          the db calls in run_in_threadpool, or move the router to
          dependencies_async. Not reported once every db use is wrapped.

Usage:
  python scripts/check_deps.py [root]          report
  python scripts/check_deps.py [root] --fix    rewrite FIX cases to def

Files whose name contains " old" are skipped.
"""

import ast
import sys
from pathlib import Path

DEPS = {"get_auth", "get_current_user", "get_supabase"}
SKIP_FILES = {"dependencies.py", "dependencies_async.py", "check_deps.py"}
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git"}


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _depends_targets(fn):
    a = fn.args
    defaults = a.defaults + [d for d in a.kw_defaults if d is not None]
    out = set()
    for d in defaults:
        if isinstance(d, ast.Call) and _name(d.func) == "Depends" and d.args:
            n = _name(d.args[0])
            if n:
                out.add(n)
    return out


THREADPOOL = {"run_in_threadpool", "to_thread"}
LOOP_CALLS = {"create_task", "ensure_future", "get_running_loop", "get_event_loop"}


def _db_params(fn) -> set[str]:
    """Parameter names bound to Depends(get_supabase)."""
    a = fn.args
    params = a.posonlyargs + a.args
    pos = list(zip(params[len(params) - len(a.defaults):], a.defaults))
    kw = [(p, d) for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
    out = set()
    for p, d in pos + kw:
        if (isinstance(d, ast.Call) and _name(d.func) == "Depends" and d.args
                and _name(d.args[0]) == "get_supabase"):
            out.add(p.arg)
    return out


def _blocking_db_use(fn) -> bool:
    """True if a db param is used outside run_in_threadpool / asyncio.to_thread.

    db.table(...) or helper(db, ...) directly in an async def runs on the
    event loop. Inside run_in_threadpool(helper, db, ...) or a lambda passed
    to it, it runs in a worker thread and is fine.
    """
    names = _db_params(fn)
    if not names:
        return False

    safe = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _name(node.func) in THREADPOOL:
            for arg in list(node.args) + [k.value for k in node.keywords]:
                for sub in ast.walk(arg):
                    safe.add(id(sub))

    for node in ast.walk(fn):
        if (isinstance(node, ast.Name) and node.id in names
                and isinstance(node.ctx, ast.Load) and id(node) not in safe):
            return True
    return False


def _needs_async(fn) -> bool:
    """True if the body awaits, uses async for/with, or yields.

    Nested functions count too: conservative, never converts a function
    whose meaning would change.
    """
    for node in ast.walk(fn):
        if node is fn:
            continue
        if isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith,
                             ast.Yield, ast.YieldFrom)):
            return True
        # need the running loop: in a def (worker thread) there is none
        if isinstance(node, ast.Call) and _name(node.func) in LOOP_CALLS:
            return True
    return False


def check(path: Path):
    """Return [(line, kind, message, fn_name_or_None)]."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (SyntaxError, UnicodeDecodeError) as e:
        print(f"{path}: skipped ({type(e).__name__})")
        return []

    issues = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func) in DEPS:
            issues.append((node.lineno, "ERROR",
                           f"direct call {_name(node.func)}() — use Depends", None))

        if isinstance(node, ast.AsyncFunctionDef) and "get_supabase" in _depends_targets(node):
            if _needs_async(node):
                if not _blocking_db_use(node):
                    continue   # awaits, and every db use is off the loop
                issues.append((node.lineno, "MANUAL",
                               f"async def {node.name} awaits — run_in_threadpool or dependencies_async",
                               None))
            else:
                issues.append((node.lineno, "FIX",
                               f"async def {node.name} has no await — make it def",
                               node.name))

    # one report per line+message (two calls on one line)
    seen, out = set(), []
    for i in sorted(issues):
        if (i[0], i[2]) not in seen:
            seen.add((i[0], i[2]))
            out.append(i)
    return out


def fix(path: Path, issues) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    for line, kind, _, fn in issues:
        if kind != "FIX":
            continue
        old = f"async def {fn}("
        idx = line - 1
        if old in lines[idx]:
            lines[idx] = lines[idx].replace(old, f"def {fn}(", 1)
            changed += 1
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_fix = "--fix" in sys.argv
    root = Path(args[0] if args else ".")

    counts = {"ERROR": 0, "FIX": 0, "MANUAL": 0}
    fixed = 0

    for path in sorted(root.rglob("*.py")):
        if path.name in SKIP_FILES or " old" in path.name:
            continue
        if any(p in SKIP_DIRS for p in path.parts):
            continue

        issues = check(path)
        for line, kind, msg, _ in issues:
            if do_fix and kind == "FIX":
                continue
            print(f"{path}:{line}: {kind}: {msg}")
            counts[kind] += 1

        if do_fix:
            fixed += fix(path, issues)

    print()
    if do_fix:
        print(f"fixed (async def -> def): {fixed}")
    for k, v in counts.items():
        print(f"{k:7} {v}")

    sys.exit(1 if counts["ERROR"] or counts["MANUAL"] or counts["FIX"] else 0)


if __name__ == "__main__":
    main()