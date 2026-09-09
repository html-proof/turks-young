from __future__ import annotations

import asyncio
import sys
from pathlib import Path
import re
import asyncpg

# Ensure root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.core import config

DATABASE_URL = config.DATABASE_URL


async def run_migrations() -> None:
    print(f"Connecting to Supabase PostgreSQL at aws-0-ap-southeast-1...")
    conn = await asyncpg.connect(DATABASE_URL, ssl="require", timeout=15)
    print("Connected successfully!")

    # 1. Ensure supabase_migrations schema and schema_migrations table exist
    await conn.execute("CREATE SCHEMA IF NOT EXISTS supabase_migrations;")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS supabase_migrations.schema_migrations (
            version TEXT PRIMARY KEY,
            statements TEXT[],
            name TEXT,
            created_by TEXT,
            idempotency_key TEXT,
            rollback TEXT
        );
    """)

    applied_rows = await conn.fetch("SELECT version FROM supabase_migrations.schema_migrations;")
    applied_versions = {r["version"] for r in applied_rows}
    print(f"Found {len(applied_versions)} migrations already applied in Supabase.")

    # 2. Iterate through migration files in order
    migrations_dir = Path(__file__).parent.parent / "supabase" / "migrations"
    migration_files = sorted(migrations_dir.glob("*.sql"))

    for migration_file in migration_files:
        filename = migration_file.name
        match = re.match(r"^(\d+)_?(.*)\.sql$", filename)
        if not match:
            continue
        version = match.group(1)
        name = match.group(2)

        if version in applied_versions:
            print(f"  [ALREADY APPLIED] {filename}")
            continue

        print(f"  [APPLYING] {filename} ...")
        sql_content = migration_file.read_text(encoding="utf-8")

        async with conn.transaction():
            # Execute migration DDL
            await conn.execute(sql_content)
            # Record in supabase_migrations table
            await conn.execute(
                """
                INSERT INTO supabase_migrations.schema_migrations (version, name)
                VALUES ($1, $2)
                ON CONFLICT (version) DO UPDATE SET name = EXCLUDED.name;
                """,
                version,
                name,
            )
        print(f"  [SUCCESS] Applied {filename}")

    # 3. Also execute schema.sql idempotently to guarantee full schema alignment
    schema_path = Path(__file__).parent.parent / "api" / "db" / "schema.sql"
    if schema_path.exists():
        print(f"Applying schema.sql to ensure full alignment...")
        schema_sql = schema_path.read_text(encoding="utf-8")
        await conn.execute(schema_sql)
        print("schema.sql applied successfully.")

    # 4. Verify tables in public schema
    rows = await conn.fetch("""
        SELECT table_name
          FROM information_schema.tables
         WHERE table_schema = 'public'
         ORDER BY table_name;
    """)
    table_names = [r["table_name"] for r in rows]
    print("\nCurrent tables in Supabase public schema:")
    for t in table_names:
        print(f"  - {t}")

    await conn.close()
    print("\nDatabase push to Supabase completed successfully!")


if __name__ == "__main__":
    asyncio.run(run_migrations())
