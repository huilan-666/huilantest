"""两阶段经营案例：不可变来源、本地核实和实际资源事实分开。"""
import copy
import hashlib
import json
from datetime import date, datetime, timedelta

from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone

from .models import Audit, Batch, BusinessCase, CaseAssessment, CaseSnapshot, ResourceEvent, ResourceRequest
from .policy import profile, require, scoped
from .services import DomainError, MAX_BYTES, MAX_ROWS, TZ, date_value

PHASES = {'prequote': '商机到报价', 'registered': '注册到首交'}
REASONS = {'contract': '合同/签约', 'demand': '暂无需求/场景待挖掘', 'demand_changed': '需求变化',
           'competitor': '竞品', 'price': '价格', 'compliance': '方案合规疑虑',
           'kp_access': '核心KP未达', 'kp_changed': 'KP改变决定', 'unknown': '未知/待核实'}
PRIORITIES = {'resource': '针对性资源候选', 'decision': '补决策承诺', 'future': '未来计划与关系核实',
              'clarify': '原因具体化', 'feedback': '注册后更新反馈', 'unassigned': '待分组', 'done': '已推进对照'}
RESOURCE_TYPES = {'expert': '合规专家', 'price': '价格/首测审批', 'management': '管理层协助',
                  'kp': 'BD/渠道KP建联', 'business': '业务核实'}
OUTCOMES = {'pending': '待推进', 'quoted': '已报价推进', 'crm_traded': 'CRM已交易观察',
            'first_trade': '已核实首次成功交易', 'unknown': '结果未知'}
VERDICTS = {'pending': '待核实', 'confirmed': '已确认', 'rejected': '已否定'}
EVENTS = {'intervention': '实际介入', 'feedback': '客户反馈', 'obstacle': '障碍变化',
          'cancel': '取消协助', 'review1': '第一轮复盘', 'review2': '第二轮复盘'}
OBSTACLES = {'resolved': '已解除', 'partial': '部分解除', 'unresolved': '未解除', 'unknown': '未知'}
LIMITATIONS = ['当前为所选来源覆盖的案例，不代表全公司转化率。', '缺稳定关联、同龄对照和完整结果，不能判断资源净贡献。',
               '请求不等于实际介入，介入后交易仅说明时间先后；前后变化不证明因果。']


class CaseConflict(DomainError):
    def __init__(self, version):
        super().__init__('记录已更新，输入已保留，请核对当前版本后重试。', 'conflict', 409)
        self.current_version = version


def text(value, required=False, limit=20000):
    if value is None:
        value = ''
    if not isinstance(value, str) or len(value) > limit or required and not value.strip():
        raise DomainError('文本必填项或长度不符合要求。', 'format_error')
    return value.strip()


def day(value, label='日期', required=False, past=False):
    if value in [None, '']:
        if required:
            raise DomainError(label + '必须填写。')
        return None
    result = date_value(value, label)
    if past and result > timezone.localdate(timezone=TZ):
        raise DomainError(label + '不能晚于今天。')
    return result


def real_time(value):
    if not isinstance(value, str):
        raise DomainError('真实时刻必须为含时区的ISO8601格式。', 'format_error')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise DomainError('真实时刻格式无效。', 'format_error') from exc
    if result.utcoffset() is None:
        raise DomainError('真实时刻缺少时区。', 'format_error')
    return result.astimezone(TZ)


def enum(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise DomainError(label + '不是有效选项。', 'format_error')
    return value


def validate_snapshot(payload):
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DomainError('经营案例返回不是合法JSON数据。', 'format_error') from exc
    if len(encoded) > MAX_BYTES or not isinstance(payload, dict) or payload.get('type') != 'cases':
        raise DomainError('经营案例类型或处理预算不符合协议。', 'format_error')
    data = copy.deepcopy(payload)
    for key, limit in [('source', 200), ('source_label', 200), ('coverage', 2000)]:
        data[key] = text(data.get(key), True, limit)
    if type(data.get('complete')) is not bool or not isinstance(data.get('cases'), list) or len(data['cases']) > MAX_ROWS:
        raise DomainError('完整性标记或案例数组不符合协议。', 'format_error')
    cutoff = real_time(data['source_cutoff']) if data.get('source_cutoff') else None
    observed = day(data.get('observed_on'))
    if not observed and not cutoff or observed and cutoff and observed != cutoff.date():
        raise DomainError('来源截止缺失或日期与真实时刻冲突。', 'format_error')
    observed = observed or cutoff.date()
    data['observed_on'] = observed.isoformat()
    if cutoff:
        data['source_cutoff'] = cutoff.isoformat()
    keys = set()
    for item in data['cases']:
        if not isinstance(item, dict):
            raise DomainError('案例必须为对象。', 'format_error')
        for key in ['source_key', 'enterprise', 'opportunity']:
            item[key] = text(item.get(key), True, 200 if key == 'source_key' else 500)
        if item['source_key'] in keys:
            raise DomainError('来源内source_key重复。', 'format_error')
        keys.add(item['source_key'])
        enum(item.get('phase'), PHASES, '阶段')
        item['department'] = text(item.get('department', ''), limit=200)
        for key in ['registered_on', 'outcome_on', 'future_on', 'next_review_on']:
            day(item.get(key), key)
        for key in ['registered_evidence', 'outcome_evidence', 'migration_evidence', 'priority_reason',
                    'suggested_resource_detail', 'next_result', 'future_quote', 'source_note', 'owner']:
            if key in item:
                text(item[key])
        item['outcome'] = enum(item.get('outcome', 'unknown'), OUTCOMES, '结果')
        item['migration'] = enum(item.get('migration', 'unknown'), ['unknown', 'natural', 'excluded'], '迁移')
        if item['migration'] == 'excluded' and not item.get('migration_evidence'):
            raise DomainError('迁移排除缺少具体记录依据。', 'format_error')
        if item['outcome'] in ['quoted', 'crm_traded', 'first_trade'] and not item.get('outcome_evidence'):
            raise DomainError('阶段结果缺少来源依据。', 'format_error')
        outcome_day = day(item.get('outcome_on'))
        if item['outcome'] in ['quoted', 'crm_traded', 'first_trade'] and outcome_day and outcome_day > observed:
            raise DomainError('结果发生日期晚于来源截止，不能将计划当作已发生结果。', 'validation_failed')
        item['priority_group'] = enum(item.get('priority_group', 'unassigned'), PRIORITIES, '推进分组')
        if not item.get('priority_reason') and item['priority_group'] != 'done':
            item['priority_group'] = 'unassigned'
        if item.get('suggested_resource'):
            enum(item['suggested_resource'], RESOURCE_TYPES, '建议资源')
        for key in ['blockers', 'evidence', 'contradictions']:
            if not isinstance(item.get(key, []), list):
                raise DomainError('证据或阻塞列表格式无效。', 'format_error')
        for blocker in item.get('blockers', []):
            if not isinstance(blocker, dict):
                raise DomainError('阻塞格式无效。', 'format_error')
            enum(blocker.get('code'), REASONS, '阻塞')
            text(blocker.get('detail', '')); text(blocker.get('evidence', ''))
            if item['phase'] == 'registered' and any(word in (blocker.get('detail', '') + blocker.get('evidence', '')).lower()
                                                    for word in ['kyc不通过', 'kyc审核失败', '开户资料审核不通过']):
                raise DomainError('注册后不能将注册前KYC失败作为核心阻塞。', 'format_error')
        for evidence in item.get('evidence', []):
            if not isinstance(evidence, dict):
                raise DomainError('证据条目格式无效。', 'format_error')
            for key in ['quote', 'source_ref', 'product', 'related_opportunity']:
                text(evidence.get(key, ''))
            for key in ['occurred_on', 'recorded_on']:
                day(evidence.get(key), key)
            for key in ['occurred_at', 'recorded_at']:
                if evidence.get(key):
                    real_time(evidence[key])
        for value in item.get('contradictions', []):
            text(value)
        for key in ['kp', 'plan', 'forecast']:
            if not isinstance(item.get(key, {}), dict):
                raise DomainError('KP、计划或预计字段格式无效。', 'format_error')
        kp, plan = item.get('kp', {}), item.get('plan', {})
        for key in ['identity_evidence', 'evidence']:
            text(kp.get(key, ''))
        for key in ['date_text', 'amount_text', 'currency', 'purpose', 'evidence']:
            text(plan.get(key, ''))
        if 'priority_order' in item and (type(item['priority_order']) is not int or item['priority_order'] < 0):
            raise DomainError('来源排序序号格式无效。', 'format_error')
        enum(kp.get('identity', 'unknown'), ['boss', 'shareholder', 'unknown'], 'KP身份')
        for key in ['phone', 'wechat', 'reachable', 'willing']:
            enum(kp.get(key, 'unknown'), ['yes', 'no', 'unknown'], 'KP触达')
        day(kp.get('occurred_on')); day(plan.get('occurred_on'))
        enum(plan.get('kp_confirmation', 'unknown'), ['kp_said', 'kp_confirmed', 'unknown'], '计划确认')
        if not isinstance(kp.get('signals', []), list):
            raise DomainError('成交信号格式无效。', 'format_error')
        for signal in kp.get('signals', []):
            if not isinstance(signal, dict):
                raise DomainError('成交信号格式无效。', 'format_error')
            text(signal.get('text', '')); day(signal.get('occurred_on'))
            enum(signal.get('speaker', 'unknown'), ['kp_said', 'kp', 'reported', 'unknown'], '信号来源')
    fingerprint = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return data, observed, cutoff, fingerprint


def publish_cases(payload):
    data, observed, cutoff, fingerprint = validate_snapshot(payload)
    if not data['complete'] or not data['cases']:
        return None, False
    with transaction.atomic():
        latest = CaseSnapshot.objects.filter(source=data['source']).order_by('-version').first()
        if latest and (observed < latest.observed_on or cutoff and latest.source_cutoff and cutoff < latest.source_cutoff):
            raise DomainError('来源截止倒灌，保留上次有效案例快照。', 'validation_failed')
        existing = CaseSnapshot.objects.filter(fingerprint=fingerprint).first()
        if existing:
            return existing, False
        snapshot = CaseSnapshot.objects.create(source=data['source'], source_label=data['source_label'], observed_on=observed,
                                               source_cutoff=cutoff, time_precision='timestamp' if cutoff else 'day',
                                               coverage=data['coverage'], version=latest.version + 1 if latest else 1,
                                               fingerprint=fingerprint, payload=data)
        for item in data['cases']:
            case = BusinessCase.objects.filter(source=snapshot.source, source_key=item['source_key']).first()
            if case:
                BusinessCase.objects.filter(pk=case.pk).update(current_snapshot=snapshot, data=item,
                    department=case.department_override if case.department_override is not None else item.get('department', ''), version=F('version') + 1)
            else:
                BusinessCase.objects.create(source=snapshot.source, source_key=item['source_key'], current_snapshot=snapshot,
                    first_snapshot=snapshot, data=item, department=item.get('department', ''))
    return snapshot, True


def visible_cases(user):
    return scoped(BusinessCase.objects.all(), user)


def get_case(user, case_id):
    try:
        return visible_cases(user).select_related('current_snapshot', 'first_snapshot').get(pk=case_id)
    except BusinessCase.DoesNotExist as exc:
        raise Http404 from exc


def choose_snapshot(user, snapshot_id=None):
    snapshots = CaseSnapshot.objects.order_by('-pk')
    if snapshot_id:
        try:
            snapshot = snapshots.get(pk=snapshot_id)
        except (CaseSnapshot.DoesNotExist, ValueError) as exc:
            raise Http404 from exc
        if not (user.is_superuser or profile(user).role in ['ops', 'ceo']) and not visible_cases(user).filter(source=snapshot.source, first_snapshot__version__lte=snapshot.version).exists():
            raise Http404
        return snapshot
    ordered = list(snapshots)
    ordered.sort(key=lambda s: (not s.source.startswith('historical-'), s.pk), reverse=True)
    for snapshot in ordered:
        if user.is_superuser or profile(user).role in ['ops', 'ceo'] or visible_cases(user).filter(source=snapshot.source, first_snapshot__version__lte=snapshot.version).exists():
            return snapshot
    return None


def source_context(snapshot, user):
    company = user.is_superuser or profile(user).role in ['ops', 'ceo']
    return {'id': snapshot.pk, 'source': snapshot.source if company else '', 'source_label': snapshot.source_label if company else '本部门授权案例来源',
            'coverage': snapshot.coverage if company else '仅本部门授权可见案例；不含未知归属及其他部门',
            'observed_on': snapshot.observed_on.isoformat(), 'source_cutoff': snapshot.source_cutoff.isoformat() if snapshot.source_cutoff else '',
            'time_precision': snapshot.time_precision, 'version': snapshot.version,
            'is_historical': snapshot.observed_on < timezone.localdate(timezone=TZ),
            'source_kind': 'historical' if snapshot.source.startswith('historical-') else 'crm'}


def snapshot_case_data(case, snapshot):
    if snapshot.source != case.source or snapshot.version < case.first_snapshot.version:
        raise Http404
    for item in snapshot.payload['cases']:
        if item['source_key'] == case.source_key:
            return copy.deepcopy(item), True
    for older in CaseSnapshot.objects.filter(source=case.source, version__lt=snapshot.version).order_by('-version'):
        for item in older.payload['cases']:
            if item['source_key'] == case.source_key:
                return copy.deepcopy(item), False
    raise Http404


def kp_status(value):
    complete = value.get('identity') in ['boss', 'shareholder'] and value.get('identity_evidence') and value.get('evidence') and value.get('occurred_on') and all(value.get(k) == 'yes' for k in ['phone', 'wechat', 'reachable', 'willing'])
    return {'valid': bool(complete), 'label': '有效触达（按证据日期）' if complete else '有效触达待核实', 'status': 'confirmed' if complete else 'unknown'}


def plan_status(value):
    complete = all(value.get(k) for k in ['date_text', 'amount_text', 'currency', 'purpose', 'evidence', 'occurred_on']) and value.get('kp_confirmation') in ['kp_said', 'kp_confirmed']
    return {'valid': bool(complete), 'label': '计划清晰（按证据日期）' if complete else '计划待核实', 'status': 'confirmed' if complete else 'unknown'}


def completed(data):
    return data.get('outcome') in ['crm_traded', 'first_trade'] or data.get('phase') == 'prequote' and data.get('outcome') == 'quoted'


def waiting_status(data, observed):
    base = {'days': None, 'level': 'unknown', 'label': '等待时长未知', 'reason': '', 'calendar': '中国2026年官方日历；注册当日不计、源截止日计入；日级检查线索'}
    if data['phase'] != 'registered' or completed(data) or data.get('migration') == 'excluded':
        return {**base, 'level': 'not_applicable', 'label': '不适用注册后门槛', 'reason': '报价前、已推进或明确迁移不套门槛'}
    start = day(data.get('registered_on'))
    if not start or not data.get('registered_evidence'):
        return {**base, 'reason': '缺有效注册日期或注册阶段依据'}
    if start > observed:
        return {**base, 'reason': '注册日期晚于来源截止'}
    if start.year != 2026 or observed.year != 2026:
        return {**base, 'reason': '工作日历未覆盖该年份'}
    holidays = set()
    for a, b in [('01-01','01-03'), ('02-15','02-23'), ('04-04','04-06'), ('05-01','05-05'),
                 ('06-19','06-21'), ('09-25','09-27'), ('10-01','10-07')]:
        current, last = date.fromisoformat('2026-' + a), date.fromisoformat('2026-' + b)
        while current <= last:
            holidays.add(current); current += timedelta(days=1)
    workends = {date.fromisoformat('2026-' + s) for s in ['01-04','02-14','02-28','05-09','09-20','10-10']}
    count, current = 0, start + timedelta(days=1)
    while current <= observed:
        if current in workends or current.weekday() < 5 and current not in holidays:
            count += 1
        current += timedelta(days=1)
    level = 'priority' if count > 5 else 'check' if count > 3 else 'normal'
    return {**base, 'days': count, 'level': level, 'label': {'priority': '重点排查', 'check': '检查', 'normal': '正常等待'}[level], 'reason': '按来源截止冻结计算'}


def effective_case(case, snapshot):
    data, covered = snapshot_case_data(case, snapshot)
    source_data = copy.deepcopy(data)
    assessments = list(case.assessments.select_related('author', 'based_snapshot').order_by('-pk'))
    current = [a for a in assessments if a.based_snapshot_id == snapshot.pk]
    for target in ['kp', 'plan', 'priority', 'outcome']:
        latest = next((a for a in current if a.target == target), None)
        if latest and latest.verdict == 'confirmed':
            if target in ['kp', 'plan']:
                data[target] = copy.deepcopy(latest.value)
            else:
                data.update(latest.value)
        elif latest and latest.verdict == 'rejected' and target == 'outcome':
            data.update(outcome='unknown', outcome_on=None, outcome_evidence='本地核实已否定来源结果：' + latest.evidence)
        elif latest and latest.verdict == 'rejected' and target == 'priority':
            data.update(priority_group='unassigned', priority_reason='本地已否定原推进方向，需重新核实：' + latest.evidence)
    blockers = {b['code']: {**b, 'verdict': 'pending', 'label': REASONS[b['code']], 'verdict_label': VERDICTS['pending']} for b in data.get('blockers', [])}
    seen = set()
    for assessment in current:
        if assessment.target == 'blocker' and assessment.category not in seen:
            code = assessment.category; seen.add(code)
            blockers[code] = {**blockers.get(code, {'code': code, 'detail': ''}), 'label': REASONS[code], 'verdict': assessment.verdict,
                              'verdict_label': VERDICTS[assessment.verdict], 'evidence': assessment.evidence}
    if not blockers and not completed(data):
        blockers['unknown'] = {'code': 'unknown', 'label': REASONS['unknown'], 'detail': '缺少可核实的当前阻塞', 'evidence': '', 'verdict': 'pending', 'verdict_label': VERDICTS['pending']}
    data.update(id=case.pk, version=case.version, source_key=case.source_key, department=case.department,
                department_label=case.department or '部门待核实', source_department=source_data.get('department', ''),
                phase_label=PHASES[data['phase']], outcome_label=OUTCOMES[data.get('outcome', 'unknown')],
                is_covered=covered, source_changed=case.current_snapshot_id != snapshot.pk or any(a.based_snapshot_id != snapshot.pk for a in assessments),
                is_completed=completed(data), blockers=list(blockers.values()))
    group = 'done' if data['is_completed'] else data.get('priority_group', 'unassigned')
    data.update(priority_group=group, priority_label=PRIORITIES[group], priority_group_label=PRIORITIES[group],
                suggested_resource_label=RESOURCE_TYPES.get(data.get('suggested_resource'), '待确定'),
                waiting=waiting_status(data, snapshot.observed_on), conflicts=data.get('contradictions', []),
                next_review=data.get('next_review_on') or '待约定', detail_url=f'/cases/{case.pk}/?snapshot={snapshot.pk}')
    kp, plan = copy.deepcopy(data.get('kp', {})), copy.deepcopy(data.get('plan', {}))
    kp.update(kp_status(kp)); plan.update(plan_status(plan))
    for target, value in [('kp', kp), ('plan', plan)]:
        latest = next((a for a in current if a.target == target), None)
        if latest and latest.verdict == 'rejected':
            value.update(valid=False, status='rejected', label='本地核实已否定，需补证据')
    data['kp'], data['plan'] = kp, plan
    return data, source_data, assessments


def resource_data(resource, user):
    events = [{'id': e.pk, 'kind': e.kind, 'label': EVENTS[e.kind], 'occurred_on': e.occurred_on.isoformat(),
               'actor': e.actor, 'evidence': e.evidence, 'payload': e.payload, 'recorded_at': e.created_at.isoformat(),
               'author': e.author.first_name or e.author.username} for e in resource.events.select_related('author').order_by('occurred_on', 'pk')]
    reviews = []
    for number, due in [(1, resource.first_review_on), (2, resource.second_review_on)]:
        actual = [e for e in events if e['kind'] == 'review' + str(number)]
        reviews.append({'round': number, 'due_on': due.isoformat(), 'done': bool(actual), 'events': actual,
                        'overdue': due < timezone.localdate(timezone=TZ) and not actual})
    return {'id': resource.pk, 'version': resource.version, 'problem': resource.problem, 'resource_type': resource.resource_type,
            'resource_label': RESOURCE_TYPES[resource.resource_type], 'owner': resource.owner, 'requested_on': resource.requested_on.isoformat(),
            'recorded_at': resource.created_at.isoformat(), 'author': resource.author.first_name or resource.author.username,
            'next_step': resource.next_step, 'expected_feedback': resource.expected_feedback,
            'first_review_on': resource.first_review_on.isoformat(), 'second_review_on': resource.second_review_on.isoformat(),
            'status': 'cancelled' if any(e['kind'] == 'cancel' for e in events) else 'requested',
            'events': events, 'reviews': reviews, 'has_intervention': any(e['kind'] == 'intervention' for e in events),
            'has_feedback': any(e['kind'] == 'feedback' for e in events), 'source_snapshot': resource.source_snapshot_id,
            'source_context': source_context(resource.source_snapshot, user),
            'limitations': LIMITATIONS}


def assessment_summary(assessment):
    value = assessment.value
    if assessment.target == 'blocker':
        return REASONS[assessment.category]
    if assessment.target == 'kp':
        labels = {'boss': '老板', 'shareholder': '大股东', 'yes': '是', 'no': '否', 'unknown': '未知'}
        return '；'.join(label + '：' + labels.get(value.get(key), '未知') for key, label in
                        [('identity', '拍板身份'), ('phone', '有电话'), ('wechat', '有微信'), ('reachable', '可联系'), ('willing', '愿沟通')])
    if assessment.target == 'plan':
        return '日期：{}；金额：{} {}；用途：{}'.format(value.get('date_text', '未知'), value.get('amount_text', '未知'), value.get('currency', ''), value.get('purpose', '未知'))
    if assessment.target == 'priority':
        return PRIORITIES.get(value.get('priority_group'), '待分组') + '；' + value.get('priority_reason', '') + '；' + (value.get('future_quote') or '')
    if assessment.target == 'outcome':
        return OUTCOMES.get(value.get('outcome'), '未知') + '；' + (value.get('outcome_on') or '日期未知')
    return '部门归属：{} → {}'.format(value.get('previous_department') or '未知', value.get('department') or '未知')


def case_detail(user, case_id, snapshot_id=None):
    case = get_case(user, case_id)
    snapshot = choose_snapshot(user, snapshot_id) if snapshot_id else CaseSnapshot.objects.filter(source=case.source).order_by('-version').first()
    data, original, assessments = effective_case(case, snapshot)
    p = profile(user); admin = user.is_superuser
    history = [{'id': a.pk, 'target': a.target, 'category': a.category, 'verdict': a.verdict, 'verdict_label': VERDICTS[a.verdict],
                'value': a.value, 'evidence': a.evidence, 'occurred_on': a.occurred_on.isoformat(), 'based_snapshot': a.based_snapshot_id,
                'stale': a.based_snapshot_id != snapshot.pk, 'author': a.author.first_name or a.author.username,
                'summary': assessment_summary(a),
                'created_at': a.created_at.isoformat()} for a in assessments]
    versions = [{'id': s.pk, 'version': s.version, 'observed_on': s.observed_on.isoformat(),
                 'url': f'/cases/{case.pk}/?snapshot={s.pk}'} for s in CaseSnapshot.objects.filter(source=case.source, version__gte=case.first_snapshot.version).order_by('-version')]
    return {'case': data, 'source_case': original, 'source_context': source_context(snapshot, user),
            'evidence': original.get('evidence', []), 'kp': data['kp'], 'plan': data['plan'], 'forecast': original.get('forecast', {}),
            'blockers': data['blockers'], 'waiting': data['waiting'], 'assessments': history,
            'resources': [resource_data(r, user) for r in case.resources.select_related('source_snapshot').order_by('-pk')],
            'versions': versions, 'permissions': {'can_draft': admin or p.role in ['ops', 'business'],
                'can_confirm': admin or p.role == 'business', 'can_assign': admin or p.role == 'ops', 'can_resource': admin or p.role == 'business'},
            'limitations': LIMITATIONS, 'today': timezone.localdate(timezone=TZ).isoformat()}


def workbench_data(user, params):
    p = profile(user)
    view = params.get('view') or ('ops' if user.is_superuser or p.role == 'ops' else p.role)
    enum(view, ['ops', 'business', 'ceo'], '视角')
    stage = params.get('stage', 'all'); enum(stage, ['all', *PHASES], '阶段筛选')
    reason = params.get('reason', ''); enum(reason, ['', *REASONS], '阻塞筛选')
    query = str(params.get('q', '')).strip()
    snapshot = choose_snapshot(user, params.get('snapshot'))
    snapshots = []
    for s in CaseSnapshot.objects.order_by('-pk'):
        if user.is_superuser or p.role in ['ops', 'ceo'] or visible_cases(user).filter(source=s.source, first_snapshot__version__lte=s.version).exists():
            snapshots.append({**source_context(s, user), 'url': '/diagnosis/?snapshot=' + str(s.pk)})
    cases = []
    if snapshot:
        for case in visible_cases(user).filter(source=snapshot.source, first_snapshot__version__lte=snapshot.version).select_related('first_snapshot', 'current_snapshot'):
            data, _, _ = effective_case(case, snapshot)
            if stage != 'all' and data['phase'] != stage or reason and not any(b['code'] == reason for b in data['blockers']):
                continue
            if query and query.casefold() not in (' '.join([data['enterprise'], data['opportunity'], data.get('owner', '')])).casefold():
                continue
            cases.append(data)
    cases.sort(key=lambda c: (list(PRIORITIES).index(c['priority_group']), c.get('priority_order', 9999), c['id']))
    eligible = [c for c in cases if c['is_covered'] and c.get('migration') != 'excluded']
    pending = [c for c in eligible if not c['is_completed']]
    blocker_groups = []
    for phase, phase_label in PHASES.items():
        for code, label in REASONS.items():
            members = [c for c in pending if c['phase'] == phase and any(b['code'] == code for b in c['blockers'])]
            if members:
                states = {v: sum(any(b['code'] == code and b['verdict'] == v for b in c['blockers']) for c in members) for v in VERDICTS}
                blocker_groups.append({'phase': phase, 'phase_label': phase_label, 'code': code, 'label': label, 'count': len(members), **states, 'cases': members})
    priority_groups = [{'code': code, 'label': label, 'cases': [c for c in pending if c['priority_group'] == code]} for code, label in PRIORITIES.items() if code != 'done']
    chains, requests_count, intervention_count, intervened_cases = [], 0, 0, set()
    for data in cases:
        resources = [resource_data(r, user) for r in ResourceRequest.objects.filter(case_id=data['id']).select_related('source_snapshot').prefetch_related('events').order_by('pk')]
        requests_count += len(resources)
        for r in resources:
            if r['has_intervention']:
                intervention_count += 1; intervened_cases.add(data['id'])
        chains.append({'case': data, 'resources': resources, 'suggestion': data.get('suggested_resource_detail') or data.get('next_result', ''),
                       'outcome': data['outcome_label'], 'outcome_on': data.get('outcome_on'), 'outcome_evidence': data.get('outcome_evidence', ''),
                       'comparison': {'comparable': False, 'reason': '缺稳定结果关联、同观察时长群体及可比基线，不能判断资源净贡献。'}})
    return {'view': view, 'filters': {'stage': stage, 'reason': reason, 'q': query, 'snapshot': snapshot.pk if snapshot else ''},
            'sources': snapshots, 'snapshots': snapshots, 'source_context': source_context(snapshot, user) if snapshot else None,
            'counts': {'total': len(cases), 'pending': len(pending), 'completed': sum(c['is_completed'] for c in eligible),
                       'migration': sum(c.get('migration') == 'excluded' for c in cases), 'uncovered': sum(not c['is_covered'] for c in cases)},
            'cases': cases, 'blocker_groups': blocker_groups, 'priority_groups': priority_groups,
            'resource_summary': {'requested': requests_count, 'requests': requests_count, 'interventions': intervention_count,
                                 'intervened_cases': len(intervened_cases), 'chains': chains,
                                 'requested_count': requests_count, 'intervention_count': intervention_count, 'case_count': len(intervened_cases),
                                 'review_due_count': sum(review['overdue'] for chain in chains for r in chain['resources'] for review in r['reviews']),
                                 'gaps': LIMITATIONS,
                                 'message': '尚未登记实际资源介入' if not intervention_count else '实际介入与客户/阶段结果分开核对'},
            'legacy_batches': [{'id': b.pk, 'name': b.name, 'url': '/diagnosis/?batch=' + str(b.pk)} for b in Batch.objects.order_by('-pk') if user.is_superuser or p.role in ['ops', 'ceo'] or scoped(b.records.all(), user).exists()],
            'limitations': LIMITATIONS}


def _version(value):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainError('缺少有效版本。') from exc
    if result < 1:
        raise DomainError('缺少有效版本。')
    return result


def _cas_case(case, version, user, **values):
    if not visible_cases(user).filter(pk=case.pk, version=_version(version)).update(version=F('version') + 1, **values):
        raise CaseConflict(get_case(user, case.pk).version)


def save_assessment(user, case_id, payload):
    require(user, 'ops', 'business')
    case = get_case(user, case_id)
    snapshot = choose_snapshot(user, payload.get('snapshot') or case.current_snapshot_id)
    snapshot_case_data(case, snapshot)
    target = enum(payload.get('target'), ['blocker', 'kp', 'plan', 'priority', 'outcome'], '核实类型')
    verdict = enum(payload.get('verdict'), VERDICTS, '核实结论')
    if verdict != 'pending':
        require(user, 'business')
    evidence = text(payload.get('evidence'), True)
    occurred = day(payload.get('occurred_on'), '实际发生日期', True, True)
    value = payload.get('value', {})
    if not isinstance(value, dict):
        raise DomainError('核实内容格式无效。')
    value = copy.deepcopy(value)
    allowed = {
        'blocker': ['detail'],
        'kp': ['identity', 'identity_evidence', 'phone', 'wechat', 'reachable', 'willing'],
        'plan': ['date_text', 'amount_text', 'currency', 'purpose', 'kp_confirmation'],
        'priority': ['priority_group', 'priority_reason', 'future_quote', 'future_on', 'next_review_on'],
        'outcome': ['outcome', 'outcome_on', 'fund_received', 'disbursed', 'association_evidence'],
    }
    if any(key not in allowed[target] for key in value):
        raise DomainError('核实内容含不适用字段。')
    category = ''
    if target == 'blocker':
        category = enum(payload.get('category'), REASONS, '阻塞分类')
    if target == 'kp':
        for key in ['identity', 'identity_evidence', 'phone', 'wechat', 'reachable', 'willing']:
            value[key] = payload.get(key, value.get(key, 'unknown' if key != 'identity_evidence' else ''))
        enum(value['identity'], ['boss', 'shareholder', 'unknown'], 'KP身份')
        for key in ['phone', 'wechat', 'reachable', 'willing']:
            enum(value[key], ['yes', 'no', 'unknown'], '触达')
        text(value['identity_evidence'])
        value.update(evidence=evidence, occurred_on=occurred.isoformat())
        if verdict == 'confirmed' and not kp_status(value)['valid']:
            raise DomainError('确认有效KP须有老板/股东拍板依据、电话微信、可联系且愿沟通的全部依据。')
    if target == 'plan':
        for key in ['date_text', 'amount_text', 'currency', 'purpose', 'kp_confirmation']:
            value[key] = payload.get(key, value.get(key, 'unknown' if key == 'kp_confirmation' else ''))
        enum(value['kp_confirmation'], ['kp_said', 'kp_confirmed', 'unknown'], '计划确认')
        for key in ['date_text', 'amount_text', 'currency', 'purpose']:
            text(value[key])
        value.update(evidence=evidence, occurred_on=occurred.isoformat())
        if verdict == 'confirmed' and not plan_status(value)['valid']:
            raise DomainError('确认清晰计划须有日期、金额币种、用途、KP本人或确认及依据日期。')
    if target == 'priority':
        value['priority_group'] = enum(payload.get('priority_group', value.get('priority_group')), PRIORITIES, '推进分组')
        if value['priority_group'] == 'done' and not effective_case(case, snapshot)[0]['is_completed']:
            raise DomainError('已推进分组须有独立报价或交易结果依据，不能仅靠调整分组结案。')
        value['priority_reason'] = text(payload.get('priority_reason', value.get('priority_reason')), True)
        for key in ['future_quote', 'future_on', 'next_review_on']:
            value[key] = payload.get(key, value.get(key))
        text(value.get('future_quote')); day(value.get('future_on'), '需求沟通日期', past=True); day(value.get('next_review_on'), '下次复核日期')
    if target == 'outcome':
        value['outcome'] = enum(payload.get('outcome', value.get('outcome')), OUTCOMES, '阶段结果')
        outcome_date = day(payload.get('outcome_on', value.get('outcome_on')), '结果发生日期', value['outcome'] in ['quoted', 'crm_traded', 'first_trade'], True)
        value['outcome_on'] = outcome_date.isoformat() if outcome_date else None
        value['outcome_evidence'] = evidence
        if verdict == 'confirmed' and value['outcome'] == 'first_trade':
            for key in ['fund_received', 'disbursed', 'association_evidence']:
                value[key] = payload.get(key, value.get(key))
            if value['fund_received'] != 'yes' or value['disbursed'] != 'yes' or not text(value.get('association_evidence')):
                raise DomainError('首次成功交易须确认资金转入、成功下发及本商机关联依据。')
    with transaction.atomic():
        _cas_case(case, payload.get('case_version'), user)
        result = CaseAssessment.objects.create(case=case, target=target, category=category, verdict=verdict, value=value,
            evidence=evidence, occurred_on=occurred, based_snapshot=snapshot, author=user)
        Audit.objects.create(author=user, entity='case_assessment', entity_id=result.pk, department=case.department, event=verdict,
                             detail={'case_id': case.pk, 'target': target, 'snapshot': snapshot.pk})
    return result


def assign_department(user, case_id, payload):
    require(user, 'ops')
    case = get_case(user, case_id)
    department = text(payload.get('department', ''), limit=200)
    evidence = text(payload.get('evidence'), True)
    with transaction.atomic():
        _cas_case(case, payload.get('case_version'), user, department=department, department_override=department)
        result = CaseAssessment.objects.create(case=case, target='department', verdict='confirmed',
            value={'department': department, 'previous_department': case.department, 'source_department': case.data.get('department', '')},
            evidence=evidence, occurred_on=timezone.localdate(timezone=TZ), based_snapshot=case.current_snapshot, author=user)
        Audit.objects.create(author=user, entity='case_assessment', entity_id=result.pk, department=department,
                             event='department', detail={'case_id': case.pk})
    return result


def create_resource(user, case_id, payload):
    require(user, 'business')
    case = get_case(user, case_id)
    snapshot = choose_snapshot(user, payload.get('snapshot') or case.current_snapshot_id)
    snapshot_case_data(case, snapshot)
    values = {key: text(payload.get(key), True, 200 if key == 'owner' else 20000)
              for key in ['problem', 'owner', 'next_step', 'expected_feedback']}
    values['resource_type'] = enum(payload.get('resource_type'), RESOURCE_TYPES, '资源类型')
    requested = day(payload.get('requested_on'), '请求发生日期', True, True)
    first = day(payload.get('first_review_on'), '首轮复盘日期', True)
    second = day(payload.get('second_review_on'), '二轮复盘日期', True)
    if first < requested or second < first:
        raise DomainError('复盘日期需满足请求≤首轮≤二轮。')
    with transaction.atomic():
        _cas_case(case, payload.get('case_version'), user)
        resource = ResourceRequest.objects.create(case=case, source_snapshot=snapshot, requested_on=requested,
            first_review_on=first, second_review_on=second, author=user, **values)
        Audit.objects.create(author=user, entity='resource', entity_id=resource.pk, department=case.department, event='requested', detail={'case_id': case.pk})
    return resource


def save_resource_event(user, resource_id, payload):
    require(user, 'business')
    try:
        resource = ResourceRequest.objects.select_related('case').get(pk=resource_id, case__in=visible_cases(user))
    except ResourceRequest.DoesNotExist as exc:
        raise Http404 from exc
    kind = enum(payload.get('kind'), EVENTS, '资源事件')
    occurred = day(payload.get('occurred_on'), '实际发生日期', True, True)
    if occurred < resource.requested_on:
        raise DomainError('事件不能早于请求发生日期。')
    actor = text(payload.get('actor'), True, 200)
    evidence = text(payload.get('evidence'), True)
    values = {}
    if kind in ['obstacle', 'review1', 'review2']:
        values['obstacle_status'] = enum(payload.get('obstacle_status'), OBSTACLES, '障碍状态')
    if kind in ['review1', 'review2']:
        for key in ['execution', 'customer_feedback', 'result_progress']:
            values[key] = text(payload.get(key), True)
        if kind == 'review2':
            prior = resource.events.filter(kind='review1').order_by('-pk').first()
            if not prior or occurred < prior.occurred_on:
                raise DomainError('第二轮复盘需有不晚于本次的首轮记录。')
    if kind == 'cancel' and resource.events.filter(kind='cancel').exists():
        raise DomainError('事项已取消，不能重复取消。')
    cancelled = resource.events.filter(kind='cancel').order_by('-pk').first()
    if cancelled and kind == 'intervention' and occurred > cancelled.occurred_on:
        raise DomainError('已取消事项不能登记取消后的新介入。')
    with transaction.atomic():
        if not ResourceRequest.objects.filter(pk=resource.pk, case__in=visible_cases(user), version=_version(payload.get('resource_version'))).update(version=F('version') + 1):
            get_case(user, resource.case_id)
            raise CaseConflict(ResourceRequest.objects.get(pk=resource.pk).version)
        event = ResourceEvent.objects.create(resource=resource, kind=kind, occurred_on=occurred, actor=actor,
                                             evidence=evidence, payload=values, author=user)
        Audit.objects.create(author=user, entity='resource_event', entity_id=event.pk, department=resource.case.department,
                             event=kind, detail={'resource_id': resource.pk})
    return event
