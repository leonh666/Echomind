"""Durable Feishu outbox. Ticket and delivery intent commit in one transaction."""
import json
import uuid
from datetime import datetime, timezone

from business.store import BusinessError, MANAGERS


SCHEMA = """
CREATE TABLE IF NOT EXISTS feishu_deliveries (
 ticket_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
 app_id TEXT NOT NULL, app_token TEXT NOT NULL, table_id TEXT NOT NULL,
 client_token TEXT NOT NULL UNIQUE, fields_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','sending','delivered','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
 lease_token TEXT, lease_until REAL, record_id TEXT, error_code TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 FOREIGN KEY(ticket_id,organization_id) REFERENCES tickets(id,organization_id)
);
CREATE INDEX IF NOT EXISTS feishu_delivery_due ON feishu_deliveries(status,next_attempt_at);
"""

FIELD_NAMES = ('工单编号', '问题标题', '问题描述', '问题分类', '优先级', '处理状态', '创建时间', '处理备注')


def enqueue(conn, ticket, target, timestamp):
    """Only the configured organization can export, never arbitrary metadata."""
    if not target or ticket['organization_id'] != target['organization_id']:
        return
    fields = dict(zip(FIELD_NAMES, (
        ticket['id'], ticket['title'], ticket['description'],
        {'general': '一般问题', 'technical': '技术支持', 'billing': '账单问题'}[ticket['category']],
        {'low': '低', 'normal': '普通', 'high': '高', 'urgent': '紧急'}[ticket['priority']],
        {'open': '待受理', 'in_progress': '处理中', 'resolved': '已解决', 'closed': '已关闭', 'cancelled': '已取消'}[ticket['status']],
        datetime.fromtimestamp(ticket['created_at'], timezone.utc).isoformat(), '',
    )))
    conn.execute("""INSERT OR IGNORE INTO feishu_deliveries
        (ticket_id,organization_id,app_id,app_token,table_id,client_token,fields_json,
         status,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?,?)""",
        (ticket['id'], ticket['organization_id'], target['app_id'], target['app_token'], target['table_id'],
         str(uuid.uuid4()), json.dumps(fields, ensure_ascii=False), timestamp, timestamp, timestamp))


def same_target(row, target):
    return bool(target) and all(row[key] == target[key] for key in ('organization_id', 'app_id', 'app_token', 'table_id'))


class DeliveryStore:
    def __init__(self, store, target):
        self.store, self.target = store, target

    def statuses(self, actor, tickets):
        with self.store._transaction() as conn:
            role = self.store._actor(conn, actor)
            result = {}
            for ticket in tickets:
                row = self.store._visible(conn.execute('SELECT * FROM tickets WHERE id=?', (ticket['id'],)).fetchone(), actor, role)
                delivery = conn.execute('SELECT * FROM feishu_deliveries WHERE ticket_id=?', (row['id'],)).fetchone()
                if delivery:
                    state = delivery['status'] if same_target(delivery, self.target) else 'target_changed'
                    result[row['id']] = {'status': state, 'attempts': delivery['attempts'],
                        'updated_at': delivery['updated_at'], 'error_code': delivery['error_code']}
                else:
                    enabled = self.target and row['organization_id'] == self.target['organization_id']
                    result[row['id']] = {'status': 'not_queued' if enabled else 'disabled', 'attempts': 0}
            return result

    def retry(self, actor, ticket_id):
        with self.store._transaction() as conn:
            role = self.store._actor(conn, actor)
            if role not in MANAGERS:
                raise BusinessError('forbidden', '只有管理员或客服可以重试飞书同步', 403)
            ticket = self.store._visible(conn.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone(), actor, role)
            if not self.target or actor.organization_id != self.target['organization_id']:
                raise BusinessError('feishu_disabled', '当前组织尚未启用飞书工单同步', 409)
            row = conn.execute('SELECT * FROM feishu_deliveries WHERE ticket_id=?', (ticket_id,)).fetchone()
            timestamp = self.store.now()
            if row and not same_target(row, self.target):
                raise BusinessError('feishu_target_changed', '该工单绑定的飞书表格已变更，请管理员核对原表格配置', 409)
            if ticket['status'] in {'closed', 'cancelled'} and (not row or row['status'] != 'delivered'):
                raise BusinessError('ticket_not_active', '已关闭或取消的工单不能同步至飞书', 409)
            if not row:
                enqueue(conn, ticket, self.target, timestamp)
            elif row['status'] in {'delivered', 'sending', 'queued'}:
                return
            else:
                conn.execute("UPDATE feishu_deliveries SET status='queued',next_attempt_at=?,error_code='',updated_at=? WHERE ticket_id=?",
                             (timestamp, timestamp, ticket_id))
            self.store._event(conn, actor, 'ticket.feishu_queued', ticket_id=ticket_id, task_id=ticket['source_task_id'])

    def claim(self):
        if not self.target:
            return None
        with self.store._transaction() as conn:
            now = self.store.now()
            conn.execute("""UPDATE feishu_deliveries SET status='failed',error_code='ticket_not_active',updated_at=?
                WHERE status='queued' AND organization_id=? AND ticket_id IN
                (SELECT id FROM tickets WHERE status IN ('closed','cancelled'))""", (now, self.target['organization_id']))
            row = conn.execute("""SELECT * FROM feishu_deliveries WHERE organization_id=? AND app_id=? AND app_token=? AND table_id=?
                AND ((status='queued' AND next_attempt_at<=?) OR (status='sending' AND lease_until<=?))
                ORDER BY created_at,ticket_id LIMIT 1""", (*[self.target[k] for k in ('organization_id', 'app_id', 'app_token', 'table_id')], now, now)).fetchone()
            if not row:
                return None
            lease = uuid.uuid4().hex
            conn.execute("UPDATE feishu_deliveries SET status='sending',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE ticket_id=?",
                         (lease, now + 180, now, row['ticket_id']))
            return dict(conn.execute('SELECT * FROM feishu_deliveries WHERE ticket_id=?', (row['ticket_id'],)).fetchone())

    def finish(self, job, record_id='', error_code='', retryable=False):
        with self.store._transaction() as conn:
            now = self.store.now()
            status = 'delivered' if record_id else ('queued' if retryable and job['attempts'] < 5 else 'failed')
            conn.execute("""UPDATE feishu_deliveries SET status=?,record_id=?,error_code=?,next_attempt_at=?,
                lease_token=NULL,lease_until=NULL,updated_at=? WHERE ticket_id=? AND lease_token=? AND status='sending'""",
                (status, record_id or None, error_code, now + min(300, 15 * 2 ** min(job['attempts'] - 1, 5)),
                 now, job['ticket_id'], job['lease_token']))
