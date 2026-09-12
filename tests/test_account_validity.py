import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from firebase_admin import auth
from firebase_admin.exceptions import UnavailableError

from api.accounts import router
from api.auth import get_current_user, get_optional_user
from api.firebase import FirebaseRuntime


@pytest.fixture
def session():
    app = FastAPI()
    app.include_router(router)
    runtime = SimpleNamespace(enabled=True, verify_id_token=AsyncMock(return_value={
        'uid': 'firebase-a', 'auth_time': int(time.time())}))
    repo = SimpleNamespace(get_session_account=AsyncMock(return_value={'uid': 'uuid-a', 'account_status': 'active'}))
    app.state.firebase = runtime
    app.state.user_repository = repo
    app.state.account_deletion = SimpleNamespace(delete_account=AsyncMock())

    @app.get('/protected')
    async def protected(user=Depends(get_current_user)):
        return {'uid': user.uid}

    @app.get('/optional')
    async def optional(user=Depends(get_optional_user)):
        return {'guest': user is None}

    return TestClient(app), app, runtime, repo


def request(client, path='/session/validate', **kwargs):
    return client.get(path, headers={'Authorization': 'Bearer test-token'}, **kwargs)


def test_active_account_returns_only_authoritative_uuid(session):
    client, _, runtime, repo = session
    response = request(client)
    assert response.json() == {'valid': True, 'status': 'ACTIVE', 'user_id': 'uuid-a'}
    repo.get_session_account.assert_awaited_once_with('firebase-a')


def test_no_local_id_can_authorize_request(session):
    client, _, runtime, _ = session
    assert client.get('/protected?user_id=uuid-a').status_code == 401
    runtime.verify_id_token.assert_not_awaited()


@pytest.mark.parametrize('state,reason', [
    (None, 'USER_NOT_FOUND'), ('disabled', 'ACCOUNT_DISABLED'),
    ('suspended', 'ACCOUNT_DISABLED'), ('deleted', 'ACCOUNT_DELETED'),
    ('deletion_pending', 'ACCOUNT_DELETED')])
def test_database_invalidity_blocks_every_protected_request(session, state, reason):
    client, _, _, repo = session
    repo.get_session_account.return_value = None if state is None else {'uid': 'uuid-a', 'account_status': state}
    for path in ['/protected', '/session/validate', '/optional']:
        response = request(client, path)
        assert response.status_code == 401
        assert response.json()['detail'] == {'code': 'ACCOUNT_INVALID', 'reason': reason}


@pytest.mark.parametrize('error,reason', [
    (auth.UserNotFoundError('missing'), 'USER_NOT_FOUND'),
    (auth.UserDisabledError('disabled'), 'ACCOUNT_DISABLED'),
    (auth.RevokedIdTokenError('revoked'), 'TOKEN_REVOKED'),
    (auth.InvalidIdTokenError('invalid'), 'INVALID_TOKEN')])
def test_firebase_invalidity_is_positive_and_stable(session, error, reason):
    client, _, runtime, repo = session
    runtime.verify_id_token.side_effect = error
    response = request(client)
    assert response.status_code == 401
    assert response.json()['detail']['reason'] == reason
    repo.get_session_account.assert_not_awaited()


def test_expiration_is_refreshable(session):
    client, _, runtime, _ = session
    runtime.verify_id_token.side_effect = auth.ExpiredIdTokenError('expired', None)
    assert request(client).json()['detail']['code'] == 'TOKEN_EXPIRED'


def test_firebase_service_failure_does_not_identify_account_as_deleted(session):
    client, _, runtime, _ = session
    runtime.verify_id_token.side_effect = UnavailableError('temporary')
    response = request(client)
    assert response.status_code == 503
    assert 'ACCOUNT_INVALID' not in response.text


def test_admin_deletion_invalidates_an_already_active_device(session):
    client, _, _, repo = session
    assert request(client).status_code == 200
    repo.get_session_account.return_value = None
    assert request(client, '/protected').status_code == 401
    # Another device's startup makes the same authoritative validation call.
    assert request(client).status_code == 401


def test_self_deletion_ignores_client_identity(session):
    client, app, _, _ = session
    response = client.delete('/account?firebase_uid=victim', headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 200
    app.state.account_deletion.delete_account.assert_awaited_once_with('firebase-a')


def test_admin_route_requires_verified_admin_claim(session):
    client, app, runtime, _ = session
    assert client.delete('/admin/accounts/victim', headers={'Authorization': 'Bearer test-token'}).status_code == 403
    app.state.account_deletion.delete_account.assert_not_awaited()
    runtime.verify_id_token.return_value['admin'] = True
    assert client.delete('/admin/accounts/victim', headers={'Authorization': 'Bearer test-token'}).status_code == 200
    app.state.account_deletion.delete_account.assert_awaited_once_with('victim')


def test_sensitive_deletion_requires_recent_signin(session):
    client, app, runtime, _ = session
    runtime.verify_id_token.return_value['auth_time'] = 1
    response = client.delete('/account', headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 403
    assert response.json()['detail']['code'] == 'REAUTHENTICATION_REQUIRED'
    app.state.account_deletion.delete_account.assert_not_awaited()


def test_admin_sdk_explicitly_checks_revocation(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, 'verify_id_token', lambda *args, **kwargs: calls.append(kwargs) or {'uid': 'a'})
    runtime = FirebaseRuntime(None)
    runtime.app = object()
    asyncio.run(runtime.verify_id_token('token'))
    assert calls == [{'check_revoked': True}]
