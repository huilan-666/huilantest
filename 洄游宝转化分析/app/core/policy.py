from django.core.exceptions import PermissionDenied
from django.db.models import Q
from .models import Profile


def profile(user):
    if not user.is_authenticated:
        raise PermissionDenied('请先登录。')
    try:
        return Profile.objects.get(user=user)
    except Profile.DoesNotExist as exc:
        raise PermissionDenied('账号尚未配置业务权限。') from exc


def department_allowed(p, department):
    return p.user.is_superuser or p.role in ['ops', 'ceo'] or any(department == d or department.startswith(d + '-') for d in p.departments)


def scoped(queryset, user, field='department'):
    p = profile(user)
    if user.is_superuser or p.role in ['ops', 'ceo']:
        return queryset
    filters = Q(pk__in=[])
    for d in p.departments:
        filters |= Q(**{field: d}) | Q(**{field + '__startswith': d + '-'})
    return queryset.filter(filters)


def require(user, *roles):
    p = profile(user)
    if not user.is_superuser and p.role not in roles:
        raise PermissionDenied('当前角色没有此操作权限。')
    return p
