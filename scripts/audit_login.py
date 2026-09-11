"""Read-only identity diagnostics; never prints emails, tokens or secrets."""
import asyncio
import json
import os
from pathlib import Path
import asyncpg
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from api.core import config
from api.firebase import FirebaseRuntime
from firebase_admin import auth

async def main():
    runtime = FirebaseRuntime.from_environment()
    runtime.initialize()
    users = list(auth.list_users(app=runtime.app).iterate_all())
    print('Firebase auth accounts:', len(users))
    print('Google-linked accounts:', sum(any(p.provider_id == 'google.com' for p in u.provider_data) for u in users))
    conn = await asyncpg.connect(config.DATABASE_URL, ssl='require', timeout=15, statement_cache_size=0)
    rows = await conn.fetch('SELECT uid, firebase_uid, email FROM users')
    print('Database accounts:', len(rows))
    print('Firebase accounts without database link:', sum(not any(r['firebase_uid'] == u.uid for r in rows) for u in users))
    await conn.close()
    info = json.loads(config.FIREBASE_SERVICE_ACCOUNT_JSON) if config.FIREBASE_SERVICE_ACCOUNT_JSON else json.loads(Path(os.environ['GOOGLE_APPLICATION_CREDENTIALS']).read_text())
    session = AuthorizedSession(service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/cloud-platform']))
    base = 'https://firebase.googleapis.com/v1beta1/projects/' + info['project_id']
    result = session.get(base + '/androidApps/1:668570569113:android:cd085423f982d547bceb08/sha', timeout=20)
    print('Firebase certificate query status:', result.status_code)
    if result.ok:
        print('Registered public certificate hashes:', [(x.get('certType'), x.get('shaHash')) for x in result.json().get('certificates', [])])

if __name__ == '__main__':
    asyncio.run(main())
