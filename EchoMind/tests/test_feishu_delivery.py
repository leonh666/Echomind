"""Real SQLite outbox and mocked official HTTP API; no Feishu account/network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from business.feishu_delivery import DeliveryStore, FIELD_NAMES
from business.store import BusinessError, BusinessStore, Principal
from integrations.feishu import FeishuClient, FeishuConfig, FeishuError, FeishuService


CONFIG = FeishuConfig(True, 'a', 'cli_test', 'test-feishu-secret', 'bas_test', 'tbl_test')
PAYLOAD = {'title': '退款未到账', 'description': '申请后仍未到账，需要人工核查', 'category': 'billing',
           'metadata': {'password': 'never-export-this', 'app_token': 'forged-target'}}


@pytest.fixture
def setup(tmp_path):
    clock = [1000.0]
    store = BusinessStore(tmp_path / 'feishu.sqlite3', now=lambda: clock[0], feishu_target=CONFIG.target)
    with store._transaction() as db:
        for org in ('a', 'b'):
            db.execute('INSERT INTO organizations VALUES(?,?,?)', (org, org, clock[0]))
        for uid, org, role in (('alice', 'a', 'customer'), ('bob', 'a', 'customer'),
                               ('support', 'a', 'support'), ('other', 'b', 'admin')):
            db.execute('INSERT INTO users(id,email,display_name,password_hash,password_salt,password_iterations,created_at) VALUES(?,?,?,?,?,?,?)',
                       (uid, uid + '@example.invalid', uid, 'hash', '00', 1, clock[0]))
            db.execute('INSERT INTO memberships VALUES(?,?,?)', (uid, org, role))
    return store, clock, Principal('alice', 'a', 'customer')


def confirmed(store, actor):
    draft = store.create_draft(actor, PAYLOAD)
    return store.confirm_draft(actor, draft['id'], draft['revision'], draft['payload_hash'], draft['idempotency_key'])


def rows(store):
    with store._transaction() as db:
        return [dict(row) for row in db.execute('SELECT * FROM feishu_deliveries')]


def test_confirm_enqueues_once_atomically_and_only_exports_allowlisted_fields(setup):
    store, _, actor = setup
    draft = store.create_draft(actor, PAYLOAD)
    assert rows(store) == []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.confirm_draft(actor, draft['id'], 1, draft['payload_hash'], draft['idempotency_key']), range(6)))
    assert len({item['ticket']['id'] for item in results}) == 1
    delivery, = rows(store)
    fields = json.loads(delivery['fields_json'])
    assert set(fields) == set(FIELD_NAMES)
    assert fields['处理状态'] == '待受理'
    assert fields['工单编号'] == results[0]['ticket']['id']
    assert 'never-export-this' not in delivery['fields_json']
    assert 'forged-target' not in delivery['fields_json']
    assert delivery['app_token'] == CONFIG.app_token


def test_outbox_failure_rolls_back_ticket_creation_and_approval_consumption(setup, monkeypatch):
    from business import feishu_delivery
    store, _, actor = setup
    draft = store.create_draft(actor, PAYLOAD)
    def broken(*args):
        raise RuntimeError('simulated disk failure')
    monkeypatch.setattr(feishu_delivery, 'enqueue', broken)
    with pytest.raises(RuntimeError):
        store.confirm_draft(actor, draft['id'], 1, draft['payload_hash'], draft['idempotency_key'])
    assert store.get_task(actor, draft['id'])['status'] == 'waiting_confirmation'
    assert store.list_tickets(actor) == []


def test_disabled_other_org_and_cancelled_drafts_never_enqueue(setup):
    store, _, actor = setup
    confirmed(store, Principal('other', 'b', 'admin'))
    draft = store.create_draft(actor, PAYLOAD)
    store.cancel_task(actor, draft['id'], 1)
    store.feishu_target = None
    confirmed(store, actor)
    assert rows(store) == []


def test_claim_is_exclusive_and_restart_recovers_expired_lease(setup):
    store, clock, actor = setup
    confirmed(store, actor)
    delivery = DeliveryStore(store, CONFIG.target)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: delivery.claim(), range(4)))
    first, = [job for job in claimed if job]
    restarted = DeliveryStore(BusinessStore(store.db_path, now=lambda: clock[0]), CONFIG.target)
    assert restarted.claim() is None
    clock[0] += 181
    second = restarted.claim()
    assert second['client_token'] == first['client_token']
    assert second['lease_token'] != first['lease_token']
    delivery.finish(first, record_id='rec_stale')
    assert rows(store)[0]['status'] == 'sending'
    restarted.finish(second, record_id='rec_live')
    assert rows(store)[0]['record_id'] == 'rec_live'


def test_transient_retries_back_off_and_stop_after_five_attempts(setup):
    store, clock, actor = setup
    confirmed(store, actor)
    delivery = DeliveryStore(store, CONFIG.target)
    for attempt in range(5):
        job = delivery.claim()
        assert job['attempts'] == attempt + 1
        delivery.finish(job, error_code='http_429', retryable=True)
        assert delivery.claim() is None
        clock[0] += 500
    assert rows(store)[0]['status'] == 'failed'
    assert delivery.claim() is None
    token = rows(store)[0]['client_token']
    delivery.retry(Principal('support', 'a', 'support'), job['ticket_id'])
    assert delivery.claim()['client_token'] == token


def test_retry_is_scoped_and_never_redirects_an_existing_job(setup):
    store, _, actor = setup
    ticket = confirmed(store, actor)['ticket']
    delivery = DeliveryStore(store, CONFIG.target)
    with pytest.raises(BusinessError) as err:
        delivery.retry(actor, ticket['id'])
    assert err.value.http_status == 403
    with pytest.raises(BusinessError) as err:
        delivery.retry(Principal('other', 'b', 'admin'), ticket['id'])
    assert err.value.http_status == 404
    changed = DeliveryStore(store, replace(CONFIG, table_id='tbl_changed').target)
    assert changed.claim() is None
    assert changed.statuses(actor, [ticket])[ticket['id']]['status'] == 'target_changed'
    with pytest.raises(BusinessError, match='变更'):
        changed.retry(Principal('support', 'a', 'support'), ticket['id'])
    with pytest.raises(BusinessError):
        delivery.statuses(Principal('bob', 'a', 'customer'), [ticket])


def test_delivered_retry_does_not_reopen_or_overwrite_manual_processing(setup):
    store, _, actor = setup
    ticket = confirmed(store, actor)['ticket']
    delivery = DeliveryStore(store, CONFIG.target)
    delivery.finish(delivery.claim(), record_id='rec_done')
    delivery.retry(Principal('support', 'a', 'support'), ticket['id'])
    assert delivery.claim() is None
    assert rows(store)[0]['record_id'] == 'rec_done'


def test_cancelled_ticket_is_not_sent_from_pending_queue(setup):
    store, _, actor = setup
    ticket = confirmed(store, actor)['ticket']
    support = Principal('support', 'a', 'support')
    store.update_ticket_status(support, ticket['id'], 'cancelled', 1)
    delivery = DeliveryStore(store, CONFIG.target)
    assert delivery.claim() is None
    assert rows(store)[0]['error_code'] == 'ticket_not_active'
    with pytest.raises(BusinessError, match='已关闭或取消'):
        delivery.retry(support, ticket['id'])


def test_unknown_organization_fails_configuration(setup):
    store, _, _ = setup
    with pytest.raises(ValueError, match='existing organization'):
        FeishuService(store, replace(CONFIG, organization_id='typo'), AsyncMock())


def test_service_persists_safe_error_without_losing_ticket(setup, caplog):
    store, _, actor = setup
    ticket = confirmed(store, actor)['ticket']
    fake = AsyncMock()
    fake.deliver.side_effect = FeishuError('api_999', 'safe message')
    service = FeishuService(store, CONFIG, fake)
    assert asyncio.run(service.run_once())
    assert rows(store)[0]['status'] == 'failed'
    assert rows(store)[0]['error_code'] == 'api_999'
    assert service.annotate(actor, [ticket])[0]['feishu_sync']['status'] == 'failed'
    assert store.get_ticket(actor, ticket['id'])['status'] == 'open'
    assert CONFIG.app_secret not in caplog.text


def test_official_http_contract_and_lost_create_response_reconcile(setup):
    store, _, actor = setup
    confirmed(store, actor)
    delivery = DeliveryStore(store, CONFIG.target)
    job = delivery.claim()
    created, requests = [], []
    async def scenario():
        def handle(req):
            requests.append(req)
            if req.url.path.endswith('/tenant_access_token/internal'):
                assert json.loads(req.content) == {'app_id': CONFIG.app_id, 'app_secret': CONFIG.app_secret}
                return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'tenant-test', 'expire': 7200})
            assert req.headers['authorization'] == 'Bearer tenant-test'
            if req.url.path.endswith('/records/search'):
                assert json.loads(req.content)['filter']['conditions'][0]['value'] == [job['ticket_id']]
                return httpx.Response(200, json={'code': 0, 'data': {'items': created, 'has_more': False}})
            assert req.url.path.endswith('/records')
            assert req.url.params['client_token'] == job['client_token']
            assert set(json.loads(req.content)['fields']) == set(FIELD_NAMES)
            created.append({'record_id': 'rec_once', 'fields': {'工单编号': job['ticket_id']}})
            raise httpx.ReadTimeout('response lost after remote commit', request=req)
        async with httpx.AsyncClient(base_url='https://open.feishu.cn', transport=httpx.MockTransport(handle)) as transport:
            client = FeishuClient(CONFIG, transport)
            with pytest.raises(FeishuError) as error:
                await client.deliver(job)
            assert error.value.retryable
            assert await client.deliver(job) == 'rec_once'
        assert len(created) == 1
        assert len([r for r in requests if r.url.path.endswith('/records')]) == 1
        assert len([r for r in requests if r.url.path.endswith('/tenant_access_token/internal')]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('bad_fields', [False, True])
def test_configuration_check_is_read_only_and_requires_text_fields(bad_fields):
    async def scenario():
        def handle(req):
            if req.url.path.endswith('/tenant_access_token/internal'):
                return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'test', 'expire': 7200})
            assert req.method == 'GET' and req.url.path.endswith('/fields')
            return httpx.Response(200, json={'code': 0, 'data': {'items': [
                {'field_name': name, 'type': 3 if bad_fields and name == '处理状态' else 1} for name in FIELD_NAMES], 'has_more': False}})
        async with httpx.AsyncClient(base_url='https://open.feishu.cn', transport=httpx.MockTransport(handle)) as transport:
            client = FeishuClient(CONFIG, transport)
            if bad_fields:
                with pytest.raises(FeishuError, match='处理状态'):
                    await client.check()
            else:
                assert (await client.check())['ok']
    asyncio.run(scenario())


def test_provider_error_body_cannot_leak_secret():
    async def scenario():
        async with httpx.AsyncClient(base_url='https://open.feishu.cn', transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={'code': 999, 'msg': CONFIG.app_secret}))) as transport:
            with pytest.raises(FeishuError) as error:
                await FeishuClient(CONFIG, transport)._token()
            assert CONFIG.app_secret not in str(error.value)
            assert error.value.code == 'api_999'
    asyncio.run(scenario())


def test_config_defaults_disabled_and_incomplete_settings_fail_without_echoing_values(monkeypatch):
    monkeypatch.setenv('ECHOMIND_FEISHU_ENABLED', 'false')
    assert FeishuConfig.from_env().target is None
    assert CONFIG.app_secret not in repr(CONFIG)
    monkeypatch.setenv('ECHOMIND_FEISHU_ENABLED', 'true')
    for suffix in ('ORGANIZATION_ID', 'APP_ID', 'APP_SECRET', 'APP_TOKEN', 'TABLE_ID'):
        monkeypatch.delenv('ECHOMIND_FEISHU_' + suffix, raising=False)
    with pytest.raises(ValueError, match='incomplete'):
        FeishuConfig.from_env()
    for name in ('organization_id', 'app_id', 'app_secret', 'app_token', 'table_id'):
        monkeypatch.setenv('ECHOMIND_FEISHU_' + name.upper(), getattr(CONFIG, name))
    assert FeishuConfig.from_env() == CONFIG
