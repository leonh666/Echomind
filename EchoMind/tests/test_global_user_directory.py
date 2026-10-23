"""System account read access does not grant cross-organization business access."""
import pytest
from api import operations
from business.auth import AuthService
from test_admin_api import admin_api, PASSWORD
from test_admin_store import admin_data


def test_global_directory_counts_accounts_once_and_includes_unassigned_accounts(admin_data):
    admin, store, actors, _ = admin_data
    with store._transaction() as conn:
        conn.execute('INSERT INTO users(id,email,display_name,password_hash,password_salt,password_iterations,created_at,username) VALUES(?,?,?,?,?,?,?,?)',
                     ('unassigned', 'unassigned@example.invalid', 'Unassigned', 'PRIVATE_HASH', 'PRIVATE_SALT', 600000, 1001, 'unassigned'))
    page = admin.users(actors['admin-a'], limit=100)
    assert page['total'] == 8
    assert len({item['id'] for item in page['items']}) == 8
    assert next(item for item in page['items'] if item['id'] == 'unassigned')['memberships'] == []
    assert len(next(item for item in page['items'] if item['id'] == 'admin-a')['memberships']) == 2
    assert 'PRIVATE_' not in str(page)
    assert admin.overview(actors['admin-a'])['counts']['users'] == page['total']
    assert admin.users(actors['admin-b'], limit=100)['items'] == page['items']
    assert admin.users(actors['admin-a'], limit=2, offset=999)['items'] == []


def test_admin_can_see_self_registered_account_without_moving_its_data(admin_api):
    client, store = admin_api.client, admin_api.store
    registered = AuthService(store).register('self_signup', PASSWORD)
    headers = admin_api.headers['admin-a']
    page = client.get('/admin/users?organization_id=forged', headers=headers).json()
    account = next(item for item in page['items'] if item['username'] == 'self_signup')
    assert account['memberships'] == [{'organization_id': 'self-service', 'organization_name': 'EchoMind 客户工作区', 'role': 'customer'}]
    assert page['total'] == 8
    assert client.get('/admin/overview', headers=headers).json()['counts']['users'] == 8
    # Listing a foreign profile grants neither a different role nor foreign records.
    assert client.get('/tickets/' + admin_api.tickets['foreign']['id'], headers=headers).status_code == 404
    with store._transaction() as conn:
        memberships = conn.execute('SELECT organization_id,role FROM memberships WHERE user_id=?', (registered['user']['id'],)).fetchall()
    assert [(r['organization_id'], r['role']) for r in memberships] == [('self-service', 'customer')]


@pytest.mark.parametrize('uid', ['alice', 'support', 'kb'])
def test_global_directory_does_not_accept_forged_admin_parameters(admin_api, uid):
    result = admin_api.client.get('/admin/users?role=admin&scope=system', headers=admin_api.headers[uid])
    assert result.status_code == 403
    assert 'items' not in result.json()


def test_deleted_admin_membership_revokes_existing_session_global_read(admin_api):
    headers = admin_api.headers['admin-a']
    assert admin_api.client.get('/admin/users', headers=headers).status_code == 200
    with admin_api.store._transaction() as conn:
        conn.execute("UPDATE memberships SET role='customer' WHERE user_id='admin-a' AND organization_id='a'")
    assert admin_api.client.get('/admin/users', headers=headers).status_code == 403
