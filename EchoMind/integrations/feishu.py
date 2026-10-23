"""Narrow, native Python integration with Feishu Base's official OpenAPI."""
import asyncio
import argparse
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

import httpx

from business.feishu_delivery import DeliveryStore, FIELD_NAMES


logger = logging.getLogger(__name__)


class FeishuError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


@dataclass(frozen=True)
class FeishuConfig:
    enabled: bool = False
    organization_id: str = ''
    app_id: str = ''
    app_secret: str = field(default='', repr=False)
    app_token: str = ''
    table_id: str = ''

    @classmethod
    def from_env(cls):
        enabled = os.getenv('ECHOMIND_FEISHU_ENABLED', 'false').strip().lower()
        if enabled not in {'true', 'false'}:
            raise ValueError('ECHOMIND_FEISHU_ENABLED must be true or false')
        if enabled == 'false':
            return cls()
        names = ('ORGANIZATION_ID', 'APP_ID', 'APP_SECRET', 'APP_TOKEN', 'TABLE_ID')
        values = {name.lower(): os.getenv('ECHOMIND_FEISHU_' + name, '').strip() for name in names}
        missing = [name for name in names if not values[name.lower()]]
        if missing:
            raise ValueError('Feishu configuration is incomplete: ' + ', '.join('ECHOMIND_FEISHU_' + name for name in missing))
        for name in ('organization_id', 'app_id', 'app_token', 'table_id'):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', values[name]):
                raise ValueError('Invalid Feishu configuration field: ' + name)
        return cls(enabled=True, **values)

    @property
    def target(self):
        if not self.enabled:
            return None
        return {key: getattr(self, key) for key in ('organization_id', 'app_id', 'app_token', 'table_id')}


class FeishuClient:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or httpx.AsyncClient(base_url='https://open.feishu.cn', timeout=10, follow_redirects=False)
        self._owns_client = client is None
        self._access_token, self._token_until = '', 0
        self._token_lock = asyncio.Lock()

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method, path, *, authenticated=True, **kwargs):
        headers = {'Authorization': 'Bearer ' + await self._token()} if authenticated else {}
        try:
            response = await self.client.request(method, path, headers=headers, **kwargs)
        except httpx.RequestError:
            raise FeishuError('network_error', '连接飞书失败，已保留工单等待重试', True) from None
        if response.status_code != 200:
            raise FeishuError('http_' + str(response.status_code), '飞书服务暂不可用或访问权限不足', response.status_code == 429 or response.status_code >= 500)
        try:
            body = response.json()
        except ValueError:
            raise FeishuError('invalid_response', '飞书返回了无效响应', True) from None
        if not isinstance(body, dict) or type(body.get('code')) is not int:
            raise FeishuError('invalid_response', '飞书返回了无效响应', True)
        if body['code'] != 0:
            code = body['code']
            if code in {99991661, 99991663, 99991664, 99991668}:
                self._token_until = 0
            # Never persist or expose provider response text, request bodies or credentials.
            raise FeishuError('api_' + str(code), '飞书调用失败，请核对应用权限、表格授权和字段配置',
                              code in {99991400, 99991661, 99991663, 99991664, 99991668, 1254290, 1254291, 1255001, 1255040})
        return body

    async def _token(self):
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_until:
                return self._access_token
            body = await self._request('POST', '/open-apis/auth/v3/tenant_access_token/internal', authenticated=False,
                json={'app_id': self.config.app_id, 'app_secret': self.config.app_secret})
            token, expiry = body.get('tenant_access_token'), body.get('expire')
            if not isinstance(token, str) or not token or type(expiry) is not int or expiry <= 0:
                raise FeishuError('invalid_token_response', '未能获取有效的飞书应用凭证')
            self._access_token, self._token_until = token, time.monotonic() + max(1, expiry - 60)
            return token

    @property
    def table_path(self):
        return '/open-apis/bitable/v1/apps/' + quote(self.config.app_token, safe='') + '/tables/' + quote(self.config.table_id, safe='')

    async def check(self):
        fields, page_token = {}, ''
        for _ in range(20):
            params = {'page_size': 100}
            if page_token:
                params['page_token'] = page_token
            body = await self._request('GET', self.table_path + '/fields', params=params)
            data = body.get('data', {})
            for item in data.get('items', []):
                fields[item.get('field_name')] = item.get('type')
            if not data.get('has_more'):
                break
            page_token = data.get('page_token')
            if not page_token:
                raise FeishuError('invalid_response', '飞书字段分页响应无效')
        else:
            raise FeishuError('too_many_fields', '表格字段过多，请使用独立的工单数据表')
        invalid = [name for name in FIELD_NAMES if fields.get(name) != 1]
        if invalid:
            raise FeishuError('invalid_fields', '请创建以下多行文本字段：' + '、'.join(invalid))
        return {'ok': True, 'message': '已连接飞书，工单字段检查通过；本次检查未写入记录'}

    async def deliver(self, job):
        import json
        # Reconcile before every write, including a restart after a lost response.
        body = await self._request('POST', self.table_path + '/records/search', params={'page_size': 2},
            json={'field_names': ['工单编号'], 'filter': {'conjunction': 'and', 'conditions': [
                {'field_name': '工单编号', 'operator': 'is', 'value': [job['ticket_id']]}]}})
        data = body.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise FeishuError('invalid_response', '飞书查询响应无效', True)
        items = data['items']
        if len(items) > 1 or data.get('has_more'):
            raise FeishuError('duplicate_records', '飞书存在重复工单编号，请人工核对')
        if items:
            record_id = items[0].get('record_id')
        else:
            body = await self._request('POST', self.table_path + '/records', params={'client_token': job['client_token']},
                                       json={'fields': json.loads(job['fields_json'])})
            record_id = body.get('data', {}).get('record', {}).get('record_id')
        if not isinstance(record_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', record_id):
            raise FeishuError('invalid_response', '飞书未返回有效记录编号', True)
        return record_id


class FeishuService:
    def __init__(self, store, config, client=None):
        self.store, self.config = store, config
        if config.enabled:
            with store._transaction() as conn:
                if not conn.execute('SELECT 1 FROM organizations WHERE id=?', (config.organization_id,)).fetchone():
                    raise ValueError('ECHOMIND_FEISHU_ORGANIZATION_ID does not match an existing organization')
        self.deliveries = DeliveryStore(store, config.target)
        self.client = client or (FeishuClient(config) if config.enabled else None)
        self.wake = asyncio.Event()
        self.task = None

    def info(self, actor):
        enabled = bool(self.config.enabled and actor.organization_id == self.config.organization_id)
        return {'enabled': enabled, 'message': '确认提交后自动同步至飞书多维表格' if enabled else '当前组织未启用飞书工单同步'}

    def annotate(self, actor, tickets):
        statuses = self.deliveries.statuses(actor, tickets)
        return [{**ticket, 'feishu_sync': statuses[ticket['id']]} for ticket in tickets]

    async def run_once(self):
        job = await asyncio.to_thread(self.deliveries.claim)
        if not job:
            return False
        try:
            record_id = await self.client.deliver(job)
        except FeishuError as exc:
            await asyncio.to_thread(self.deliveries.finish, job, error_code=exc.code, retryable=exc.retryable)
            logger.warning('Feishu delivery failed (%s)', exc.code)
        except Exception:
            # Unknown failure is visible and manually retryable, never silently lost.
            await asyncio.to_thread(self.deliveries.finish, job, error_code='internal_error')
            logger.error('Feishu delivery failed (internal_error)')
        else:
            await asyncio.to_thread(self.deliveries.finish, job, record_id=record_id)
        return True

    async def _run(self):
        while True:
            self.wake.clear()
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.error('Feishu delivery worker unavailable; pending deliveries remain in SQLite')
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    def start(self):
        if self.config.enabled and self.task is None:
            self.task = asyncio.create_task(self._run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client:
            await self.client.close()


def main():
    """Trusted local setup helper; never writes a remote record or prints secrets."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    parser = argparse.ArgumentParser(description='飞书工单配置检查（不写入表格记录）')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--organizations', action='store_true', help='只读列出本地组织编号，供管理员选择绑定范围')
    mode.add_argument('--check', action='store_true', help='检查飞书授权和表格字段，不创建记录')
    args = parser.parse_args()
    if args.organizations:
        path = Path(os.getenv('ECHOMIND_BUSINESS_DB_PATH', './data/business/echomind.sqlite3')).resolve()
        if not path.is_file():
            parser.exit(1, '业务数据库不存在，请先启动项目并创建账号\n')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
            for org_id, name in conn.execute('SELECT id,name FROM organizations ORDER BY created_at,id'):
                print(org_id + '  ' + name)
        return
    async def check():
        config = FeishuConfig.from_env()
        if not config.enabled:
            raise ValueError('请先在后端 .env 中启用 ECHOMIND_FEISHU_ENABLED')
        client = FeishuClient(config)
        try:
            print((await client.check())['message'])
        finally:
            await client.close()
    try:
        asyncio.run(check())
    except (FeishuError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
