import sys
import json
import tempfile
import threading
import types
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.html import escape

from . import crm
from .models import Action, Audit, Batch, CRMConnection, CRMReadRun, Experience, ImportPreview, Profile, Record


def snapshot():
    return {
        'source': '受控CRM协议验收提供方', 'source_cutoff': '2026-10-20T12:30:00+08:00',
        'coverage': '洄游宝；业务一部与业务二部；2026年10月1日至20日', 'complete': True,
        'headers': ['商机名称', '商机创建时间', '所属部门', '注册时间', '首次交易时间'],
        'rows': [
            {'商机名称': '安澜海运协议验收', '商机创建时间': '2026-10-12', '所属部门': '业务一部', '注册时间': '2026-10-13', '首次交易时间': None},
            {'商机名称': '稳当香港协议验收', '商机创建时间': '2026-10-10', '所属部门': '业务二部', '注册时间': '2026-10-13', '首次交易时间': None},
        ],
        'mapping': {'name': '商机名称', 'created': '商机创建时间', 'department': '所属部门', 'registered': '注册时间', 'trade': '首次交易时间'},
        'config': {'start': '2026-10-01', 'end': '2026-10-20', 'cutoff': '2026-10-20'},
    }


class CRMEntryTests(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = override_settings(MEDIA_ROOT=Path(self.directory.name), CRM_PROVIDER='crm_protocol_test_provider')
        self.config.enable()
        self.addCleanup(self.config.disable)
        self.provider = types.ModuleType('crm_protocol_test_provider')
        self.provider.health = Mock(return_value={'status': 'connected'})
        self.provider.fetch_snapshot = Mock(side_effect=lambda: snapshot())
        self.modules = patch.dict(sys.modules, {'crm_protocol_test_provider': self.provider})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.launch = patch('core.crm.launch_run')
        self.launcher = self.launch.start()
        self.addCleanup(self.launch.stop)
        self.users = {}
        for name, role, departments in [
            ('运营蕙兰', 'ops', []), ('业务一部负责人', 'business', ['业务一部']),
            ('公司负责人', 'ceo', []), ('管理员蕙兰', 'ops', []),
        ]:
            user = get_user_model().objects.create_user(username=name, first_name=name, password='CRM协议验收密码2026')
            Profile.objects.create(user=user, role=role, departments=departments)
            self.users[name] = user
        admin = self.users['管理员蕙兰']
        admin.is_superuser = True
        admin.save(update_fields=['is_superuser'])
        self.client.force_login(self.users['运营蕙兰'])

    def start(self, kind='refresh'):
        response = self.client.post('/api/crm/' + kind + '/')
        self.assertEqual(response.status_code, 202, response.content)
        return CRMReadRun.objects.get(pk=response.json()['run_id'])

    def execute(self, kind='refresh'):
        run = self.start(kind)
        crm.execute_run(run.pk)
        run.refresh_from_db()
        return run

    def summary(self):
        response = self.client.get('/api/crm/status/')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['crm']

    def test_unconfigured_and_health_check_keep_snapshots_unchanged(self):
        with override_settings(CRM_PROVIDER=''):
            self.assertEqual(self.summary()['state'], 'not_configured')
            run = self.execute('check')
            self.assertEqual(run.status, 'not_configured')
            self.assertIsNotNone(run.finished_at)
            self.assertEqual(Batch.objects.count(), 0)
        first = self.execute()
        cutoff = first.batch.config['cutoff']
        checked = self.execute('check')
        self.assertEqual(checked.status, 'success')
        self.provider.health.assert_called_once_with()
        self.assertEqual(Batch.objects.count(), 1)
        self.assertEqual(self.summary()['state'], 'connected')
        self.assertEqual(self.summary()['last_success']['batch_id'], first.batch_id)
        self.assertEqual(first.batch.config['cutoff'], cutoff)
        self.assertIsNone(checked.batch_id)

    def test_complete_publish_duplicate_and_empty_keep_original_references(self):
        first = self.execute()
        self.assertEqual(first.status, 'success')
        self.assertEqual((Batch.objects.count(), Record.objects.count()), (1, 2))
        record = first.batch.records.first()
        leader = self.users['业务一部负责人']
        action = Action.objects.create(record=record, department=record.department, creator=leader, owner=leader,
                                       task='核实首测计划', due_date='2026-11-09', second_review_date='2026-11-23')
        experience = Experience.objects.create(root_id=uuid.uuid4(), department=record.department, condition='注册后待首交',
                                               cause='待客户反馈', evidence='协议验收证据', task='核实计划', author=leader, action=action)
        experience.references.add(record)
        again = self.execute()
        self.assertEqual(self.provider.fetch_snapshot.call_count, 2)
        self.assertEqual(again.batch_id, first.batch_id)
        self.assertTrue(again.reused)
        self.assertEqual((Batch.objects.count(), Record.objects.count()), (1, 2))
        empty = snapshot()
        empty['rows'] = []
        self.provider.fetch_snapshot.side_effect = None
        self.provider.fetch_snapshot.return_value = empty
        zero = self.execute()
        self.assertEqual(zero.status, 'success_empty')
        self.assertEqual(zero.returned_count, 0)
        self.assertEqual(zero.coverage, empty['coverage'])
        self.assertIsNone(zero.batch_id)
        self.assertEqual(self.summary()['last_success']['batch_id'], first.batch_id)
        self.assertEqual((Batch.objects.count(), Record.objects.count()), (1, 2))
        self.assertEqual(Action.objects.get(pk=action.pk).record_id, record.pk)
        self.assertEqual(Experience.objects.get(pk=experience.pk).references.get().pk, record.pk)
        result = self.client.get('/api/diagnosis/?batch=' + str(first.batch_id)).json()['report']
        self.assertEqual(result['mode'], 'sample')
        self.assertEqual(result['unknown'], 2)
        self.assertEqual(self.client.get('/diagnosis/?batch=' + str(first.batch_id)).status_code, 200)
        summary = self.summary()
        self.assertEqual(summary['last_success']['count'], 2)
        self.assertIn('12:30:00', summary['last_success']['cutoff'])
        self.assertIn('+08:00', summary['last_success']['read_at'])
        self.assertTrue(summary['last_success']['is_historical'])

    def test_empty_without_old_batch_does_not_create_diagnosis(self):
        value = snapshot()
        value['rows'] = []
        self.provider.fetch_snapshot.side_effect = None
        self.provider.fetch_snapshot.return_value = value
        self.assertEqual(self.execute().status, 'success_empty')
        self.assertEqual((Batch.objects.count(), ImportPreview.objects.count()), (0, 0))
        self.assertIsNone(self.summary()['last_success'])
        self.assertEqual(self.summary()['runs'][0]['diagnosis_url'], '')

    def test_partial_invalid_and_auth_failure_preserve_last_valid_batch(self):
        first = self.execute()
        value = snapshot()
        value['complete'] = False
        self.provider.fetch_snapshot.side_effect = None
        self.provider.fetch_snapshot.return_value = value
        partial = self.execute()
        self.assertEqual((partial.status, partial.returned_count), ('partial', 2))
        self.assertEqual(partial.coverage, value['coverage'])
        invalid_values = []
        malformed = snapshot(); malformed['complete'] = 'true'; invalid_values.append((malformed, 'format_error'))
        naive = snapshot(); naive['source_cutoff'] = '2026-10-20T12:30:00'; invalid_values.append((naive, 'format_error'))
        missing_department = snapshot(); missing_department['rows'][0]['所属部门'] = ''; invalid_values.append((missing_department, 'validation_failed'))
        invalid_date = snapshot(); invalid_date['rows'][0]['注册时间'] = '不合法日期：密码token'; invalid_values.append((invalid_date, 'validation_failed'))
        blank_row = snapshot(); blank_row['rows'].append({}); invalid_values.append((blank_row, 'validation_failed'))
        late_config = snapshot(); late_config['config']['cutoff'] = '2026-10-21'; invalid_values.append((late_config, 'format_error'))
        for value, code in invalid_values:
            with self.subTest(code=code, value=value):
                self.provider.fetch_snapshot.return_value = value
                failed = self.execute()
                self.assertEqual((failed.status, failed.error_code), ('failed', code))
                self.assertIsNotNone(failed.finished_at)
        self.provider.fetch_snapshot.side_effect = crm.CRMProviderError('auth_expired')
        with self.assertLogs('core.crm', level='ERROR'):
            expired = self.execute()
        self.assertEqual((expired.status, expired.error_code), ('auth_expired', 'auth_expired'))
        self.assertEqual((Batch.objects.count(), Record.objects.count()), (1, 2))
        self.assertEqual(self.summary()['last_success']['batch_id'], first.batch_id)

    def test_permissions_admin_department_counts_and_failed_run_privacy(self):
        first = self.execute()
        self.provider.fetch_snapshot.side_effect = crm.CRMProviderError('auth_expired')
        with self.assertLogs('core.crm', level='ERROR'):
            self.execute()
        for name in ['业务一部负责人', '公司负责人']:
            self.client.force_login(self.users[name])
            page = self.client.get('/imports/')
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'id="crm-controls"')
            self.assertNotContains(page, 'data-crm-action=')
            before = CRMReadRun.objects.count()
            for kind in ['check', 'refresh']:
                self.assertEqual(self.client.post('/api/crm/' + kind + '/').status_code, 403)
            self.assertEqual(CRMReadRun.objects.count(), before)
        self.client.force_login(self.users['业务一部负责人'])
        summary = self.summary()
        self.assertFalse(summary['can_manage'])
        self.assertEqual(summary['last_success']['count'], 1)
        self.assertEqual(summary['last_success']['coverage'], '仅本部门授权可见记录')
        self.assertIsNone(summary['runs'][0]['count'])
        self.assertEqual(summary['runs'][0]['coverage'], '')
        body = str(summary)
        self.assertNotIn('业务二部', body)
        self.assertNotIn('运营蕙兰', body)
        self.assertNotIn('稳当香港', body)
        self.assertEqual(summary['runs'][1]['count'], 1)
        self.assertEqual(summary['runs'][1]['diagnosis_url'], '/diagnosis/?batch=' + str(first.batch_id))
        self.client.force_login(self.users['管理员蕙兰'])
        self.assertTrue(self.summary()['can_manage'])
        self.provider.fetch_snapshot.side_effect = None
        self.provider.fetch_snapshot.return_value = {'invalid': '管理员不能绕过核查'}
        self.assertEqual(self.execute().error_code, 'format_error')
        self.client.logout()
        self.assertEqual(self.client.get('/api/crm/status/').status_code, 401)
        self.assertEqual(self.client.post('/api/crm/refresh/').status_code, 401)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users['运营蕙兰'])
        self.assertEqual(csrf.post('/api/crm/check/').status_code, 403)

    def test_imports_ssr_escapes_provider_text_and_json_script(self):
        payload = '</script><script>window.协议注入=true</script><img src=x onerror=alert(1)>'
        value = snapshot()
        value['source'] = payload
        value['coverage'] = payload
        self.provider.fetch_snapshot.side_effect = None
        self.provider.fetch_snapshot.return_value = value
        self.assertEqual(self.execute().status, 'success')
        page = self.client.get('/imports/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="crm-controls"')
        self.assertContains(page, 'data-crm-action="check"')
        self.assertContains(page, 'data-crm-action="refresh"')
        self.assertNotContains(page, payload)
        self.assertContains(page, str(escape(payload)))
        html = page.content.decode()
        marker = '<script id="crm-initial-state" type="application/json">'
        start = html.index(marker) + len(marker)
        initial_text = html[start:html.index('</script>', start)]
        self.assertNotIn('<script', initial_text)
        self.assertNotIn('</script', initial_text)
        self.assertIn('\\u003C/script\\u003E', initial_text)
        initial = json.loads(initial_text)
        self.assertEqual(initial['last_success']['source'], payload)
        self.assertEqual(initial['last_success']['coverage'], payload)

    def test_running_duplicate_and_expired_late_response_cannot_publish(self):
        first = self.start()
        self.assertEqual(first.status, 'running')
        duplicate = self.start()
        self.assertEqual(duplicate.pk, first.pk)
        self.assertEqual(self.launcher.call_count, 1)
        self.provider.fetch_snapshot.assert_not_called()
        summary = self.summary()
        self.assertTrue(summary['busy'])
        self.assertEqual(summary['active_run_id'], first.pk)
        CRMConnection.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertFalse(self.summary()['busy'])
        first.refresh_from_db()
        self.assertEqual(first.status, 'interrupted')
        second = self.start()
        self.assertNotEqual(second.pk, first.pk)
        crm.execute_run(first.pk)
        self.assertEqual(Batch.objects.count(), 0)
        crm.execute_run(second.pk)
        second.refresh_from_db()
        self.assertEqual(second.status, 'success')
        self.assertEqual(Batch.objects.count(), 1)
        self.assertEqual(CRMReadRun.objects.get(pk=first.pk).status, 'interrupted')

    def test_storage_failure_rolls_back_preview_and_batch(self):
        run = self.start()
        with patch('core.services.Record.objects.bulk_create', side_effect=OperationalError('token=不可输出的密码')):
            with self.assertLogs('core.crm', level='ERROR') as logs:
                crm.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.error_code), ('failed', 'storage_failed'))
        self.assertEqual((Batch.objects.count(), Record.objects.count(), ImportPreview.objects.count()), (0, 0, 0))
        self.assertNotIn('不可输出', ''.join(logs.output))
        self.assertFalse(self.summary()['busy'])
        first = self.execute()
        changed = snapshot()
        changed['rows'][0]['商机名称'] = '安澜海运新记录协议验收'
        self.provider.fetch_snapshot.side_effect = None
        self.provider.fetch_snapshot.return_value = changed
        second = self.start()
        with patch('core.crm._save_result', side_effect=OperationalError('持久化中断')):
            # 失败状态本身也暂时不可写时，保留running供租约恢复，不造成功。
            with self.assertLogs('core.crm', level='ERROR'):
                crm.execute_run(second.pk)
        self.assertEqual((Batch.objects.count(), Record.objects.count()), (1, 2))
        second.refresh_from_db()
        self.assertEqual(second.status, 'running')
        self.assertEqual(self.summary()['last_success']['batch_id'], first.batch_id)
        CRMConnection.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertFalse(self.summary()['busy'])

    def test_external_errors_and_bad_values_never_leak_tokens(self):
        secret = 'Cookie=session-secret; password=不能泄露; token=hidden'
        self.provider.fetch_snapshot.side_effect = RuntimeError(secret)
        with self.assertLogs('core.crm', level='ERROR') as logs:
            failed = self.execute()
        self.assertEqual(failed.error_code, 'read_failed')
        self.assertNotIn(secret, ''.join(logs.output))
        self.assertNotIn('session-secret', str(self.summary()))
        self.assertNotIn('session-secret', str(list(Audit.objects.values('detail'))))
        self.assertIn('请求号', self.summary()['runs'][0]['message'])
        malformed = snapshot()
        malformed['rows'][0]['商机创建时间'] = secret
        self.provider.fetch_snapshot.side_effect = None
        self.provider.fetch_snapshot.return_value = malformed
        self.assertEqual(self.execute().error_code, 'validation_failed')
        self.assertNotIn(secret, str(self.summary()))
        too_large = snapshot()
        too_large['rows'][0]['商机名称'] = '字' * (crm.MAX_BYTES // 3 + 1)
        self.provider.fetch_snapshot.return_value = too_large
        self.assertEqual(self.execute().error_code, 'format_error')
        self.assertEqual(Batch.objects.count(), 0)

    def test_unknown_internal_error_is_explicit_failed_not_success(self):
        run = self.start()
        with patch('core.crm.create_preview', side_effect=RuntimeError('内部token不能泄漏')):
            with self.assertLogs('core.crm', level='ERROR') as logs:
                crm.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.error_code), ('failed', 'internal_error'))
        self.assertIn('RuntimeError', ''.join(logs.output))
        self.assertNotIn('内部token', ''.join(logs.output))
        self.assertEqual(Batch.objects.count(), 0)

    def test_background_worker_calls_provider_once_and_persists_result(self):
        entered, release = threading.Event(), threading.Event()
        def fetch():
            entered.set()
            if not release.wait(10):
                raise RuntimeError('协议测试等待超时')
            return snapshot()
        self.provider.fetch_snapshot.side_effect = fetch
        self.launch.stop()
        threads = []
        original = threading.Thread
        def thread_factory(*args, **kwargs):
            worker = original(*args, **kwargs)
            threads.append(worker)
            return worker
        try:
            with patch('core.crm.threading.Thread', side_effect=thread_factory):
                run = self.start()
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.start().pk, run.pk)
                self.assertTrue(self.summary()['busy'])
                release.set()
                threads[0].join(10)
                self.assertFalse(threads[0].is_alive())
            close_old_connections()
            run.refresh_from_db()
            self.assertEqual(run.status, 'success')
            self.assertEqual(self.provider.fetch_snapshot.call_count, 1)
            self.assertIsNotNone(self.summary()['last_success'])
        finally:
            release.set()
            for worker in threads:
                worker.join(10)
            self.launch.start()

    def test_background_late_result_cannot_replace_newer_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        def fetch():
            entered.set()
            if not release.wait(10):
                raise RuntimeError('协议测试等待超时')
            value = snapshot()
            value['rows'][0]['商机名称'] = '过期响应不得发布'
            return value
        self.provider.fetch_snapshot.side_effect = fetch
        old = self.start()
        worker = threading.Thread(target=crm.execute_run, args=(old.pk,))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            CRMConnection.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
            self.assertFalse(self.summary()['busy'])
            self.provider.fetch_snapshot.side_effect = None
            self.provider.fetch_snapshot.return_value = snapshot()
            current = self.execute()
            self.assertEqual(current.status, 'success')
            release.set()
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(CRMReadRun.objects.get(pk=old.pk).status, 'interrupted')
            self.assertEqual(Batch.objects.count(), 1)
            self.assertNotIn('过期响应', str(list(Record.objects.values('raw'))))
            self.assertEqual(self.summary()['last_success']['batch_id'], current.batch_id)
        finally:
            release.set()
            worker.join(10)
