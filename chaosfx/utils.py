import os


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def getenv(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v is not None else default
