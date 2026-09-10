import os
from typing import Callable


APP_MODE = os.getenv("APP_MODE", "client").strip().lower()

if APP_MODE not in {"internal", "client"}:
    raise RuntimeError(f"Invalid APP_MODE: {APP_MODE}")


def is_internal() -> bool:
    return APP_MODE == "internal"


def is_client() -> bool:
    return APP_MODE == "client"


def internal_route(route_decorator: Callable):
    def decorator(func):
        if is_internal():
            return route_decorator(func)

        return func

    return decorator


def client_route(route_decorator: Callable):
    def decorator(func):
        if is_client():
            return route_decorator(func)

        return func

    return decorator
