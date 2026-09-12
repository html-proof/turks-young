"""Read-only predeployment checks; never applies migrations or prints credentials."""
import asyncio
import json
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.core.config import DATABASE_URL


async def main():
    if not DATABASE_URL:
        print(json.dumps({"ready": False, "error": "DATABASE_URL not configured"}))
        return
    conn = None
    try:
        conn = await asyncpg.connect(DATABASE_URL, ssl="require", timeout=15,
                                     command_timeout=20, statement_cache_size=0)
        async with conn.transaction(readonly=True):
            tables = set(await conn.fetchval("""SELECT array_agg(tablename::text)
                FROM pg_tables WHERE schemaname='public'""") or [])
            history = await conn.fetchval("SELECT to_regclass('supabase_migrations.schema_migrations')::text")
            versions = set()
            if history:
                versions = {r['version'] for r in await conn.fetch(
                    "SELECT version FROM supabase_migrations.schema_migrations")}
            pending = [p.name for p in sorted((ROOT / 'supabase/migrations').glob('*.sql'))
                       if p.name.split('_')[0] not in versions]
            required = {'users', 'account_identity_ledger', 'account_lifecycle_epoch',
                        'account_deletion_jobs', 'user_audio_settings'}
            report = {'connected': True, 'transaction_read_only': await conn.fetchval(
                'SHOW transaction_read_only'), 'pending_migrations': pending,
                'missing_tables': sorted(required - tables)}
            if 'users' in tables:
                report['account_status_counts'] = [dict(r) for r in await conn.fetch(
                    'SELECT account_status, count(*) FROM users GROUP BY account_status')]
                report['user_foreign_keys'] = [dict(r) for r in await conn.fetch("""
                    SELECT c.conrelid::regclass::text AS child_table,
                           a.attname AS owner_column, c.confdeltype::text AS delete_action,
                           c.convalidated AS validated,
                           EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.conrelid
                               AND t.tgname='active_owner_'||a.attname
                               AND t.tgenabled IN ('O','A')) AS active_write_guard
                    FROM pg_constraint c JOIN pg_attribute a
                      ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
                    WHERE c.contype='f' AND c.confrelid='public.users'::regclass
                    ORDER BY child_table, owner_column""")]
                report['user_triggers'] = [dict(r) for r in await conn.fetch("""
                    SELECT tgname, tgenabled::text FROM pg_trigger
                    WHERE tgrelid='public.users'::regclass AND NOT tgisinternal""")]
            if {'users', 'user_audio_settings'} <= tables:
                report['orphan_audio_settings'] = await conn.fetchval("""
                    SELECT count(*) FROM user_audio_settings s
                    WHERE NOT EXISTS(SELECT 1 FROM users u WHERE u.uid=s.user_uuid)""")
            if 'account_identity_ledger' in tables:
                report['missing_identity_fingerprints'] = await conn.fetchval("""
                    SELECT count(*) FROM users u WHERE NOT EXISTS(
                      SELECT 1 FROM account_identity_ledger l
                      WHERE l.identity_hash=encode(sha256(convert_to(u.firebase_uid,'UTF8')),'hex'))""")
            if 'account_lifecycle_epoch' in tables:
                report['epoch_rows'] = await conn.fetchval('SELECT count(*) FROM account_lifecycle_epoch')
            if 'account_deletion_jobs' in tables:
                report['pending_deletion_jobs'] = await conn.fetchval('SELECT count(*) FROM account_deletion_jobs')
            report['lifecycle_table_security'] = [dict(r) for r in await conn.fetch("""
                SELECT relname, relrowsecurity FROM pg_class
                WHERE relnamespace='public'::regnamespace AND relname IN
                  ('account_identity_ledger','account_lifecycle_epoch','account_deletion_jobs')""")]
            report['account_status_constraint'] = [dict(r) for r in await conn.fetch("""
                SELECT pg_get_constraintdef(oid) AS definition, convalidated AS validated
                FROM pg_constraint WHERE conrelid='public.users'::regclass
                  AND conname='users_account_status_check'""")]
            report['backend_table_permissions'] = [dict(r) for r in await conn.fetch("""
                SELECT relname,
                    has_table_privilege(current_user, oid, 'SELECT') AS can_read,
                    has_table_privilege(current_user, oid, 'INSERT') AS can_insert,
                    has_table_privilege(current_user, oid, 'DELETE') AS can_delete,
                    (NOT row_security_active(oid)) AS bypasses_rls
                FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN
                  ('users','account_identity_ledger','account_lifecycle_epoch','account_deletion_jobs')""")]
            report['lifecycle_functions'] = [dict(r) for r in await conn.fetch("""
                SELECT p.proname, pg_get_functiondef(p.oid) AS definition
                FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.proname IN
                  ('remember_account_identity','enqueue_deleted_account','require_active_account_write')""")]
            # Schema checks are necessary, but not a substitute for staging execution tests.
            report['staging_execution_verified'] = False
            print(json.dumps(report, indent=2))
    except Exception as exc:
        print(json.dumps({'ready': False, 'error_type': type(exc).__name__,
                          'error': 'Database check failed; credentials and connection details omitted'}))
        sys.exit(1)
    finally:
        if conn:
            await conn.close()


if __name__ == '__main__':
    asyncio.run(main())
