import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from api.accounts import AccountDeletionService
from api.cache.redis_cache import RedisCache
from api.core.home_state import HOME_STALE


class Database:
    def __init__(self):
        self.users = {'firebase-a': {'uid': 'uuid-a', 'account_status': 'active'}}
        self.jobs = {}
        self.commits = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy((self.users, self.jobs))
        try:
            yield
        except BaseException:
            self.users, self.jobs = before
            raise
        else:
            self.commits += 1

    async def fetchrow(self, query, uid):
        if 'account_deletion_jobs' in query:
            return self.jobs.get(uid)
        return self.users.get(uid)

    async def execute(self, query, uid, *args):
        if query.startswith('INSERT INTO account_deletion_jobs'):
            self.jobs.setdefault(uid, {'firebase_uid': uid, 'user_id': args[0]})
        elif query.startswith('UPDATE users') and uid in self.users:
            self.users[uid]['account_status'] = 'deletion_pending'
        elif query.startswith('DELETE FROM users'):
            self.users.pop(uid, None)
        elif query.startswith('DELETE FROM account_deletion_jobs'):
            self.jobs.pop(uid, None)


@pytest.mark.parametrize('failure', ['disable', 'cache', 'firebase', None])
def test_cleanup_failure_remains_pending_and_retry_finishes(failure):
    async def run():
        db = Database()
        firebase = type('Firebase', (), {})()
        firebase.disable_and_revoke = AsyncMock()
        firebase.delete_user = AsyncMock()
        cache = type('Cache', (), {'delete_user_data': AsyncMock()})()
        service = AccountDeletionService(db, firebase, cache)
        target = {'disable': firebase.disable_and_revoke, 'cache': cache.delete_user_data,
                  'firebase': firebase.delete_user}.get(failure)
        if target:
            target.side_effect = RuntimeError('temporarily unavailable')
            with pytest.raises(RuntimeError):
                await service.delete_account('firebase-a')
            assert db.users['firebase-a']['account_status'] == 'deletion_pending'
            assert 'firebase-a' in db.jobs
            assert db.commits == 1  # barrier committed before external work
            target.side_effect = None
            await service.finish('firebase-a')
        else:
            await service.delete_account('firebase-a')
        assert db.users == {}
        assert db.jobs == {}
        await service.delete_account('firebase-a')  # repeated admin request
        assert db.users == {} and db.jobs == {}
        assert firebase.delete_user.await_args.args == ('firebase-a',)
    asyncio.run(run())


def test_cache_cleanup_uses_exact_identity_components_and_propagates_errors():
    async def run():
        cache = RedisCache('configured', 'configured')
        client = type('Redis', (), {'scan': AsyncMock(return_value=(0, [
            'home:v4:firebase-a:all', 'pulse:user:firebase-a:20',
            'home:v4:firebase-ab:all', 'catalog:public'])), 'delete': AsyncMock()})()
        cache._client = client
        cache._local = {'home:v4:firebase-a:all': (0, 0, {}), 'catalog:public': (0, 0, {})}
        await cache.delete_user_data('firebase-a')
        client.delete.assert_awaited_once_with('home:v4:firebase-a:all', 'pulse:user:firebase-a:20')
        assert list(cache._local) == ['catalog:public']
        client.scan.side_effect = RuntimeError('redis unavailable')
        with pytest.raises(RuntimeError): await cache.delete_user_data('firebase-a')
    asyncio.run(run())


def test_direct_database_deletion_job_can_finish_without_root_user():
    async def run():
        db = Database()
        db.users.clear()
        db.jobs['firebase-a'] = {'firebase_uid': 'firebase-a', 'user_id': 'uuid-a'}
        firebase = type('Firebase', (), {'disable_and_revoke': AsyncMock(), 'delete_user': AsyncMock()})()
        cache = type('Cache', (), {'delete_user_data': AsyncMock()})()
        HOME_STALE['home:v4:firebase-a:all'] = {'private': True}
        service = AccountDeletionService(db, firebase, cache)
        await service.finish('firebase-a')
        assert not db.jobs
        assert 'home:v4:firebase-a:all' not in HOME_STALE
        assert {call.args[0] for call in cache.delete_user_data.await_args_list} == {'firebase-a', 'uuid-a'}
    asyncio.run(run())
