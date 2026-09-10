import os


APP_MODE = os.getenv("APP_MODE", "client").lower()

VALID_APP_MODES = {"client", "internal"}

if APP_MODE not in VALID_APP_MODES:
    raise RuntimeError(
        f"Invalid APP_MODE: {APP_MODE}"
    )


def is_internal() -> bool:
    return APP_MODE == "internal"


def is_client() -> bool:
    return APP_MODE == "client"
