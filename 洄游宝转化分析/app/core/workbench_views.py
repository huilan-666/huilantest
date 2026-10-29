from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from . import workbench as wb
from .views import guarded, payload


def options():
    return {'view_options': [('ops', '运营负责人'), ('business', '业务负责人'), ('ceo', 'CEO')],
            'stage_options': [('all', '全部阶段'), *wb.PHASES.items()], 'reason_options': [('', '全部阻塞'), *wb.REASONS.items()],
            'priority_options': list(wb.PRIORITIES.items()), 'resource_options': list(wb.RESOURCE_TYPES.items()),
            'verdict_options': list(wb.VERDICTS.items()), 'event_options': list(wb.EVENTS.items()),
            'obstacle_options': list(wb.OBSTACLES.items()), 'outcome_options': list(wb.OUTCOMES.items()),
            'target_options': [('blocker', '阻塞'), ('kp', 'KP'), ('plan', '交易计划'), ('priority', '推进方向'), ('outcome', '阶段结果')]}


@guarded
@require_GET
def overview(request):
    result = wb.workbench_data(request.user, request.GET)
    if request.path.startswith('/api/'):
        return JsonResponse({'ok': True, 'workbench': result})
    return render(request, 'workbench.html', {'workbench': result, 'title': '三层经营工作台', 'page_id': 'PAGE-F03-01', **options()})


@guarded
@require_GET
def detail(request, pk):
    result = wb.case_detail(request.user, pk, request.GET.get('snapshot'))
    if request.path.startswith('/api/'):
        return JsonResponse({'ok': True, 'case_detail': result})
    return render(request, 'case_detail.html', {'case_detail': result, 'title': result['case']['enterprise'], 'page_id': 'PAGE-F03-02', **options()})


def save_response(request, callback, *args):
    try:
        result = callback(request.user, *args, payload(request))
    except wb.CaseConflict as exc:
        return JsonResponse({'ok': False, 'error': str(exc), 'code': exc.code, 'current_version': exc.current_version}, status=409)
    case = result.case if hasattr(result, 'case') else result.resource.case
    snapshot = case.current_snapshot_id
    return JsonResponse({'ok': True, 'redirect': f'/cases/{case.pk}/?snapshot={snapshot}'})


@guarded
@require_POST
def assessment(request, pk):
    return save_response(request, wb.save_assessment, pk)


@guarded
@require_POST
def department(request, pk):
    return save_response(request, wb.assign_department, pk)


@guarded
@require_POST
def resource(request, pk):
    return save_response(request, wb.create_resource, pk)


@guarded
@require_POST
def event(request, pk):
    return save_response(request, wb.save_resource_event, pk)
