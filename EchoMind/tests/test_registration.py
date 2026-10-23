"""Public signup, username migration and privilege boundaries with real SQLite."""
import sqlite3
import pytest
from business.auth import AuthService
from business.store import BusinessStore, BusinessError
from test_operations_api import service, PASSWORD


def test_signup_username_login_cookie_and_customer_boundaries(service):
    client, store, owner, *_ = service
    assert client.post('/auth/register', json={'username': 'New_User', 'password': PASSWORD}).status_code == 201
    result = client.post('/auth/login', json={'username': 'NEW_USER', 'password': PASSWORD})
    assert result.status_code == 200 and 'token' not in result.json()
    assert result.json()['user']['username'] == 'new_user'
    assert result.json()['role'] == 'customer'
    assert 'HttpOnly' in result.headers['set-cookie']
    assert client.get('/auth/me').json()['user']['username'] == 'new_user'
    assert client.get('/admin/overview').status_code == 403
    assert client.get('/knowledge/documents').status_code == 403
    assert client.get('/tickets').json()['items'] == []
    assert result.json()['organization_id'] != owner['organization_id']
    assert client.post('/auth/register', json={'username': 'new_USER', 'password': PASSWORD}).status_code == 409


@pytest.mark.parametrize('extra', [{'role': 'admin'}, {'organization_id': 'other'}, {'email': 'a@b.invalid'}])
def test_registration_cannot_choose_role_or_tenant(service, extra):
    client, *_ = service
    assert client.post('/auth/register', json={'username': 'valid_user', 'password': PASSWORD, **extra}).status_code == 422


@pytest.mark.parametrize('username,password', [('ab', PASSWORD), ('a b', PASSWORD), ('root@shop.invalid', PASSWORD), ('用户名', PASSWORD), ('valid', 'short')])
def test_invalid_signup(service, username, password):
    client, *_ = service
    assert client.post('/auth/register', json={'username': username, 'password': password}).status_code in (400, 422)


def test_registration_server_target_disable_rate_and_origin(service, monkeypatch):
    client, _, owner, *_ = service
    monkeypatch.setenv('ECHOMIND_REGISTRATION_ORGANIZATION_ID', owner['organization_id'])
    assert client.post('/auth/register', json={'username': 'scoped', 'password': PASSWORD}).status_code == 201
    result = client.post('/auth/login', json={'username': 'scoped', 'password': PASSWORD}).json()
    assert result['organization_id'] == owner['organization_id'] and result['role'] == 'customer'
    monkeypatch.setenv('ECHOMIND_REGISTRATION_ENABLED', 'false')
    assert client.post('/auth/register', json={'username': 'blocked', 'password': PASSWORD}).status_code == 403
    monkeypatch.setenv('ECHOMIND_REGISTRATION_ENABLED', 'true')
    assert client.post('/auth/register', json={'username': 'blocked', 'password': PASSWORD}, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
    for _ in range(8):
        assert client.post('/auth/login', json={'username': 'unknown', 'password': PASSWORD}).status_code == 401
    assert client.post('/auth/register', json={'username': 'limited', 'password': PASSWORD}).status_code == 429


def test_legacy_database_migration_keeps_password_and_resolves_collisions(tmp_path):
    path = tmp_path/'old.sqlite3'
    salt = '12' * 32
    hashed = AuthService._password(PASSWORD, salt, 1000)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT UNIQUE,display_name TEXT,password_hash TEXT,password_salt TEXT,password_iterations INTEGER,created_at REAL)')
        for index, email in enumerate(['alice@one.invalid', 'alice@two.invalid']):
            conn.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)', (str(index), email, 'Alice', hashed, salt, 1000, index))
    store = BusinessStore(path)
    with store._transaction() as conn:
        conn.execute("INSERT INTO organizations VALUES('legacy','Old workspace',0)")
        for index in range(2):
            conn.execute("INSERT INTO memberships VALUES(?,'legacy','customer')", (str(index),))
    auth = AuthService(store)
    assert auth.login('alice', PASSWORD)['user']['id'] == '0'
    assert auth.login('alice_1', PASSWORD)['user']['id'] == '1'
    assert auth.login('alice@two.invalid', PASSWORD)['user']['id'] == '1'
    BusinessStore(path)  # migration is idempotent
    with pytest.raises(BusinessError):
        auth.login('alice', 'wrong password')
