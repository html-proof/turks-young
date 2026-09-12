import asyncio
from contextlib import asynccontextmanager
import pytest
from api.personalization.repository import PostgresUserRepository

class Connection:
    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail
        self.committed = False
    @asynccontextmanager
    async def transaction(self):
        before = list(self.writes)
        try:
            yield
            self.committed = True
        except Exception:
            self.writes = before
            raise
    async def fetchrow(self, query, *args):
        return {"uid": "stable-uuid"}
    async def execute(self, query, *args):
        if self.fail and query.startswith("UPDATE users"):
            raise RuntimeError("database unavailable")
        self.writes.append((query, args))

class Pool:
    def __init__(self, conn): self.conn = conn
    @asynccontextmanager
    async def acquire(self): yield self.conn

@pytest.mark.parametrize("fail", [False, True])
def test_preferences_and_completion_commit_or_rollback_together(fail):
    conn = Connection(fail)
    repo = PostgresUserRepository(Pool(conn))
    repo._uid_cache["firebase-a"] = "stable-uuid"
    async def run():
        await repo.save_onboarding("firebase-a", ["malayalam"], ["Malayalam"], ["artist-a"])
    if fail:
        with pytest.raises(RuntimeError): asyncio.run(run())
        assert not conn.writes
        assert not conn.committed
    else:
        asyncio.run(run())
        assert conn.committed
        assert all(args[0] == "stable-uuid" for _, args in conn.writes)
        assert any("language_ids=$2" in query and "onboarding_completed=TRUE" in query for query, _ in conn.writes)
        assert any(query.startswith("UPDATE users") for query, _ in conn.writes)
