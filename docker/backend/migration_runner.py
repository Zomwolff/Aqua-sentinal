from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path("/svc/migrations")


def _dsn() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'aqua_sentinel')}"
        f":{os.environ.get('POSTGRES_PASSWORD', 'change_me')}"
        f"@{os.environ.get('POSTGRES_HOST', 'postgres')}"
        f":{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ.get('POSTGRES_DB', 'aqua_sentinel')}"
    )


async def run_migrations() -> None:
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        raise RuntimeError(f"No migration files found in {MIGRATIONS_DIR}")

    connection = await asyncpg.connect(_dsn())
    try:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        applied = {
            row["filename"]: row["checksum"]
            for row in await connection.fetch("SELECT filename, checksum FROM schema_migrations")
        }

        for migration_file in migration_files:
            sql = migration_file.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            previous_checksum = applied.get(migration_file.name)
            if previous_checksum:
                if previous_checksum != checksum:
                    raise RuntimeError(
                        f"Applied migration changed on disk: {migration_file.name}"
                    )
                continue

            print(f"migration-runner: applying {migration_file.name}", flush=True)
            async with connection.transaction():
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                    migration_file.name,
                    checksum,
                )
            print(f"migration-runner: applied {migration_file.name}", flush=True)
    finally:
        await connection.close()


async def main() -> None:
    try:
        await run_migrations()
    except Exception as exc:
        print(f"migration-runner: FAILED: {exc}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
    if len(sys.argv) > 1:
        os.execvp(sys.argv[1], sys.argv[1:])
