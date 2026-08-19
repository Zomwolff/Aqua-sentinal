import os

import asyncpg

_pool = None

_DEFAULTS = {
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "aqua_sentinel",
    "POSTGRES_USER": "aqua_sentinel",
    "POSTGRES_PASSWORD": "change_me",
}


def _env(name: str) -> str:
    return os.environ.get(name, _DEFAULTS[name])


def get_dsn() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return (
        f"postgresql://{_env('POSTGRES_USER')}:{_env('POSTGRES_PASSWORD')}"
        f"@{_env('POSTGRES_HOST')}:{_env('POSTGRES_PORT')}/{_env('POSTGRES_DB')}"
    )


async def create_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            get_dsn(), min_size=min_size, max_size=max_size
        )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not created; call create_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
