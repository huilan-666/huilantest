"""CRM只读能力的调用入口；默认不安装真实提供方。"""
import csv
import importlib
import io
import logging
import math
import threading
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from .models import Audit, CRMConnection, CRMReadRun
from .policy import profile, require, scoped
from .services import (
    FIELDS, MAX_BYTES, MAX_ROWS, TZ, DomainError, config_values,
    create_preview, date_value, normalized, publish,
)
from .workbench import publish_cases, validate_snapshot, visible_cases

logger = logging.getLogger(__name__)
LEASE_SECONDS = 300
LABELS = {
    'not_configured': '未连接', 'running': '读取中', 'connected': '连接正常',
    'auth_expired': '认证已失效', 'failed': '更新失败', 'partial': '部分完成',
    'success': '更新成功', 'success_empty': '成功读取零条', 'interrupted': '更新中断',
}
MESSAGES = {
    'not_configured': 'CRM读取能力尚未接入，请由内部维护人员绑定已安装的只读实现。',
    'running': '任务已保存，正在只读获取数据。关闭页面不会取消任务。',
    'connected': '只读连接检查通过；业务快照和源数据截止没有改变。',
    'auth_expired': 'CRM认证已失效，请由内部维护人员恢复认证后重试。',
    'read_failed': 'CRM读取未完成，请稍后重试；上次有效快照仍可查看。',
    'format_error': 'CRM返回格式不符合读取协议，本次未发布快照。',
    'validation_failed': 'CRM数据未通过日期或必要字段核查，本次未发布快照。',
    'storage_failed': '数据保存未完成，本次未发布快照，请稍后重试。',
    'internal_error': '更新处理发生内部错误，本次未发布快照，请联系内部维护人员。',
    'partial': '仅取得部分数据，本次未发布完整快照，上次有效快照仍可查看。',
    'success': '快照已核查并保存，可进入该版本诊断；不代表原因已确认或业绩已改善。',
    'success_empty': '本次成功读取零条，没有可进入诊断的新快照；上次有效快照保留。',
    'interrupted': '上次任务已超过有效运行期限并中断，可重新发起；迟到数据不会发布。',
}


class CRMProviderError(Exception):
    """提供方仅用code表达安全类别；异常正文不会保存或返回。"""
    def __init__(self, code='read_failed'):
        super().__init__(code)
        self.code = code


class SnapshotFormatError(Exception):
    pass


def _connection():
    return CRMConnection.objects.get_or_create(connection_key='default')[0]


def expire_run():
    control = _connection()
    now = timezone.now()
    if control.current_run_id and control.lease_until and control.lease_until <= now:
        with transaction.atomic():
            released = CRMConnection.objects.filter(
                pk=control.pk, current_run_id=control.current_run_id, lease_until__lte=now,
            ).update(current_run=None, lease_token='', lease_until=None)
            if released:
                CRMReadRun.objects.filter(pk=control.current_run_id, status='running').update(
                    status='interrupted', error_code='interrupted', finished_at=now,
                )


def start_run(user, kind):
    require(user, 'ops')
    if kind not in ['check', 'refresh']:
        raise ValueError('未知CRM任务类型')
    expire_run()
    token = uuid.uuid4().hex
    control = _connection()
    with transaction.atomic():
        now = timezone.now()
        acquired = CRMConnection.objects.filter(pk=control.pk, lease_token='').update(
            lease_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS),
        )
        if not acquired:
            control.refresh_from_db()
            return CRMReadRun.objects.get(pk=control.current_run_id)
        run = CRMReadRun.objects.create(
            kind=kind, request_id=uuid.uuid4().hex, requester=user, started_at=now,
        )
        CRMConnection.objects.filter(pk=control.pk, lease_token=token).update(current_run=run)
        Audit.objects.create(author=user, entity='crm_run', entity_id=run.pk, event='running',
                             detail={'kind': kind, 'request_id': run.request_id})
        transaction.on_commit(lambda: launch_run(run.pk))
    return run


def launch_run(run_id):
    threading.Thread(target=execute_run, args=(run_id,), daemon=True, name='crm-read').start()


def _provider():
    module_path = settings.CRM_PROVIDER
    if not module_path:
        return None
    try:
        provider = importlib.import_module(module_path)
    except (ImportError, ValueError):
        return None
    if not callable(getattr(provider, 'health', None)) or not callable(getattr(provider, 'fetch_snapshot', None)):
        return None
    return provider


def _lock_run(run_id):
    """SQLite用条件UPDATE取得写锁，不依赖select_for_update。"""
    now = timezone.now()
    owned = CRMConnection.objects.filter(current_run_id=run_id, lease_until__gt=now).update(
        lease_until=now + timedelta(seconds=LEASE_SECONDS),
    )
    return bool(owned and CRMReadRun.objects.filter(pk=run_id, status='running').exists())


def _save_result(run, status, error_code='', metadata=None, batch=None, reused=False, case_snapshot=None):
    run.status, run.error_code, run.finished_at = status, error_code, timezone.now()
    if metadata:
        run.source = metadata.get('source', run.source)
        run.source_cutoff = metadata.get('source_cutoff', run.source_cutoff)
        run.coverage = metadata.get('coverage', run.coverage)
        run.returned_count = metadata.get('returned_count', run.returned_count)
        run.data_type = metadata.get('data_type', run.data_type)
        run.observed_on = metadata.get('observed_on', run.observed_on)
    run.batch, run.reused, run.case_snapshot = batch, reused, case_snapshot
    run.save()
    control_values = {'current_run': None, 'lease_token': '', 'lease_until': None}
    if run.kind == 'check':
        control_values.update(check_status='connected' if status == 'success' else status,
                              checked_at=run.finished_at)
    CRMConnection.objects.filter(current_run_id=run.pk).update(**control_values)
    Audit.objects.create(author=run.requester, entity='crm_run', entity_id=run.pk,
                         event=status, detail={'request_id': run.request_id, 'error_code': error_code,
                                               'batch_id': batch.pk if batch else None, 'case_snapshot_id': case_snapshot.pk if case_snapshot else None, 'reused': reused})


def _finish(run_id, status, error_code='', metadata=None):
    run = None
    try:
        with transaction.atomic():
            if _lock_run(run_id):
                run = CRMReadRun.objects.get(pk=run_id)
                _save_result(run, status, error_code, metadata)
    except (DatabaseError, OSError):
        # 存储本身不可用时不能伪造已记录失败；保留运行状态，由租约恢复。
        logger.error('CRM结果保存失败 run_id=%s request_id=%s code=storage_failed',
                     run_id, run.request_id if run else '')


def _snapshot(value):
    if not isinstance(value, dict):
        raise SnapshotFormatError
    source, coverage = value.get('source'), value.get('coverage')
    if not isinstance(source, str) or not source.strip() or len(source) > 200:
        raise SnapshotFormatError
    if not isinstance(coverage, str) or not coverage.strip() or len(coverage) > 2000:
        raise SnapshotFormatError
    raw_cutoff = value.get('source_cutoff')
    if not isinstance(raw_cutoff, str):
        raise SnapshotFormatError
    try:
        cutoff = datetime.fromisoformat(raw_cutoff.replace('Z', '+00:00'))
    except ValueError as exc:
        raise SnapshotFormatError from exc
    if cutoff.utcoffset() is None:
        raise SnapshotFormatError
    cutoff = cutoff.astimezone(TZ)
    if type(value.get('complete')) is not bool:
        raise SnapshotFormatError
    headers, rows, mapping, config = (value.get(k) for k in ['headers', 'rows', 'mapping', 'config'])
    if not isinstance(headers, list) or not headers or any(
        not isinstance(h, str) or not h.strip() or h != h.strip() for h in headers
    ) or len(headers) != len(set(headers)):
        raise SnapshotFormatError
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise SnapshotFormatError
    if not isinstance(mapping, dict) or any(
        key not in FIELDS or not isinstance(column, str) or column and column not in headers
        for key, column in mapping.items()
    ) or not mapping.get('created') or not mapping.get('department'):
        raise SnapshotFormatError
    if not isinstance(config, dict):
        raise SnapshotFormatError
    parsed_config = config_values(config)
    if date_value(parsed_config['cutoff'], '结果截止') > cutoff.date():
        raise SnapshotFormatError
    header_set = set(headers)
    for row in rows:
        if not isinstance(row, dict) or not set(row).issubset(header_set):
            raise SnapshotFormatError
        if any(v is not None and (type(v) not in [str, int, float] or
                                 isinstance(v, float) and not math.isfinite(v)) for v in row.values()):
            raise SnapshotFormatError
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(headers)
    writer.writerows([[row.get(h) for h in headers] for row in rows])
    content = buffer.getvalue().encode('utf-8')
    if len(content) > MAX_BYTES:
        raise SnapshotFormatError
    return {
        'source': source, 'source_cutoff': cutoff, 'coverage': coverage,
        'returned_count': len(rows), 'complete': value['complete'],
        'content': content, 'mapping': mapping, 'config': parsed_config,
    }


def execute_run(run_id):
    close_old_connections()
    request_id = ''
    data_type = 'table'
    try:
        run = CRMReadRun.objects.get(pk=run_id)
        request_id = run.request_id
        if run.status != 'running':
            return
        provider = _provider()
        if provider is None:
            _finish(run_id, 'not_configured', 'not_configured')
            return
        try:
            response = provider.health() if run.kind == 'check' else provider.fetch_snapshot()
        except CRMProviderError as exc:
            code = 'auth_expired' if exc.code == 'auth_expired' else 'read_failed'
            _finish(run_id, 'auth_expired' if code == 'auth_expired' else 'failed', code)
            logger.error('CRM提供方失败 request_id=%s code=%s', run.request_id, code)
            return
        except Exception:
            _finish(run_id, 'failed', 'read_failed')
            logger.error('CRM提供方异常 request_id=%s code=read_failed', run.request_id)
            return
        if run.kind == 'check':
            if not isinstance(response, dict) or response.get('status') not in ['connected', 'auth_expired', 'failed']:
                _finish(run_id, 'failed', 'format_error')
            else:
                status = response['status']
                _finish(run_id, 'success' if status == 'connected' else status,
                        '' if status == 'connected' else 'auth_expired' if status == 'auth_expired' else 'read_failed')
            return
        if isinstance(response, dict) and response.get('type') == 'cases':
            data_type = 'cases'
            data, observed, cutoff, _ = validate_snapshot(response)
            metadata = {'source': data['source'], 'source_cutoff': cutoff, 'coverage': data['coverage'], 'returned_count': len(data['cases']), 'data_type': 'cases', 'observed_on': observed}
            if not data['complete']:
                _finish(run_id, 'partial', 'partial', metadata)
                return
            if not data['cases']:
                _finish(run_id, 'success_empty', metadata=metadata)
                return
            with transaction.atomic():
                if not _lock_run(run_id):
                    return
                run = CRMReadRun.objects.get(pk=run_id)
                snapshot, created = publish_cases(data)
                _save_result(run, 'success', metadata=metadata, case_snapshot=snapshot, reused=not created)
            return
        try:
            snapshot = _snapshot(response)
        except SnapshotFormatError:
            _finish(run_id, 'failed', 'format_error')
            return
        except DomainError:
            _finish(run_id, 'failed', 'validation_failed')
            return
        if not snapshot['complete']:
            _finish(run_id, 'partial', 'partial', snapshot)
            return
        if snapshot['returned_count'] == 0:
            _finish(run_id, 'success_empty', metadata=snapshot)
            return
        # 读取完成后才取得短写事务：核查、发布与成功记录一起提交。
        with transaction.atomic():
            if not _lock_run(run_id):
                return
            run = CRMReadRun.objects.get(pk=run_id)
            preview = create_preview(run.requester, ContentFile(snapshot['content'], name='CRM只读快照.csv'))
            if len(preview.raw_rows) != snapshot['returned_count']:
                raise DomainError('CRM返回包含缺少必要字段的空白记录。')
            preview.mapping, preview.config = snapshot['mapping'], snapshot['config']
            preview.save(update_fields=['mapping', 'config'])
            rows = normalized(preview, preview.mapping, preview.config)
            # CRM完整快照不能通过自动剔除或日期解析降级伪装核查通过。
            if any(any('无法解析' in issue or '发布前须修正' in issue for issue in row['issues']) for row in rows):
                raise DomainError('CRM记录未通过必要日期或部门核查。')
            batch, created = publish(preview, run.requester, preview.mapping, preview.config, {})
            _save_result(run, 'success', metadata=snapshot, batch=batch, reused=not created)
    except DomainError as exc:
        _finish(run_id, 'failed', 'format_error' if exc.code == 'format_error' else 'validation_failed', {'data_type': data_type})
    except (DatabaseError, OSError):
        _finish(run_id, 'failed', 'storage_failed')
        logger.error('CRM保存失败 run_id=%s request_id=%s code=storage_failed', run_id, request_id)
    except Exception as exc:
        # 程序错误显式失败；不把未知错误或外部异常正文写入业务结果。
        _finish(run_id, 'failed', 'internal_error')
        logger.error('CRM内部处理失败 run_id=%s request_id=%s code=internal_error error_type=%s',
                     run_id, request_id, type(exc).__name__)
    finally:
        connection.close()


def _display_time(value):
    return value.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S +08:00') if value else ''


def _message(run):
    key = 'connected' if run.kind == 'check' and run.status == 'success' else run.error_code or run.status
    message = MESSAGES.get(key, MESSAGES['read_failed'])
    if run.reused:
        message = '本次已重新读取，相同内容和口径复用原有效快照；未重复增加记录。'
    return message + (' 请求号：' + run.request_id if run.error_code else '')


def context_status(user):
    expire_run()
    p = profile(user)
    company = user.is_superuser or p.role in ['ops', 'ceo']
    can_manage = user.is_superuser or p.role == 'ops'
    control = _connection()
    queryset = CRMReadRun.objects.select_related('batch', 'requester').order_by('-pk')
    latest = queryset.first()
    state = latest.status if latest else 'not_configured'
    if latest and latest.kind == 'check' and latest.status == 'success':
        state = 'connected'
    if not settings.CRM_PROVIDER and state != 'running':
        state = 'not_configured'
    active = CRMReadRun.objects.filter(pk=control.current_run_id, status='running').first()
    if active:
        state = 'running'
    last_success = None
    for successful in queryset.filter(kind='refresh', status='success'):
        if successful.case_snapshot_id:
            snap = successful.case_snapshot
            count = visible_cases(user).filter(source=snap.source, source_key__in=[c['source_key'] for c in snap.payload['cases']]).count()
            diagnostic_url = '/diagnosis/?snapshot=' + str(snap.pk)
            name, version = snap.source_label, snap.version
            cutoff = _display_time(snap.source_cutoff) if snap.source_cutoff else snap.observed_on.isoformat() + '（日期精度）'
            historical = snap.observed_on < timezone.localdate(timezone=TZ)
        elif successful.batch_id:
            count = scoped(successful.batch.records.all(), user).count()
            diagnostic_url = '/diagnosis/?batch=' + str(successful.batch_id)
            name, version = successful.batch.name, successful.batch.version
            cutoff = _display_time(successful.source_cutoff)
            historical = bool(successful.source_cutoff and successful.source_cutoff.astimezone(TZ).date() < timezone.localdate(timezone=TZ))
        else:
            continue
        if not company and not count:
            continue
        last_success = {
            'batch_id': successful.batch_id, 'case_snapshot_id': successful.case_snapshot_id,
            'type': 'cases' if successful.case_snapshot_id else 'table', 'type_label': '经营案例' if successful.case_snapshot_id else '表格快照', 'name': name,
            'source': successful.source if company else 'CRM',
            'time_precision': successful.case_snapshot.time_precision if successful.case_snapshot_id else 'timestamp',
            'cutoff': cutoff, 'read_at': _display_time(successful.finished_at),
            'coverage': successful.coverage if company else '仅本部门授权可见记录',
            'count': count, 'version': version, 'diagnosis_url': diagnostic_url, 'is_historical': historical,
        }
        break
    runs = []
    for run in queryset[:20]:
        if run.case_snapshot_id:
            snap = run.case_snapshot
            visible_count = visible_cases(user).filter(source=snap.source, source_key__in=[c['source_key'] for c in snap.payload['cases']]).count()
            url = '/diagnosis/?snapshot=' + str(snap.pk)
            cutoff = _display_time(snap.source_cutoff) if snap.source_cutoff else snap.observed_on.isoformat() + '（日期精度）'
        else:
            visible_count = scoped(run.batch.records.all(), user).count() if run.batch_id else None
            url = '/diagnosis/?batch=' + str(run.batch_id) if run.batch_id else ''
            cutoff = _display_time(run.source_cutoff) if run.source_cutoff else run.observed_on.isoformat() + '（日期精度）' if run.observed_on else ''
        has_visible_batch = (run.batch_id or run.case_snapshot_id) and (company or visible_count)
        label = '连接正常' if run.kind == 'check' and run.status == 'success' else LABELS[run.status]
        runs.append({
            'id': run.pk, 'kind': run.kind, 'state': run.status, 'label': label,
            'message': _message(run), 'started_at': _display_time(run.started_at),
            'completed_at': _display_time(run.finished_at),
            'cutoff': cutoff if company or has_visible_batch else '',
            'type': run.data_type, 'type_label': '经营案例' if run.data_type == 'cases' else '表格快照',
            'source': run.source if company else 'CRM' if has_visible_batch else '',
            'count': run.returned_count if company else visible_count if has_visible_batch else None,
            'coverage': run.coverage if company else '仅本部门授权可见记录' if has_visible_batch else '',
            'requester': run.requester.first_name or run.requester.username if company else '运营',
            'reused': run.reused,
            'diagnosis_url': url if has_visible_batch else '',
        })
    return {
        'state': state, 'label': LABELS[state],
        'message': MESSAGES['not_configured'] if state == 'not_configured' else _message(active or latest),
        'can_manage': can_manage, 'busy': bool(active), 'active_run_id': active.pk if active else None,
        'last_success': last_success, 'runs': runs,
    }
