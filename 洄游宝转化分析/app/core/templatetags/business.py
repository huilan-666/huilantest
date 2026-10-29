from django import template
from core.services import timestamp

register = template.Library()

@register.filter
def at(value, key):
    return value.get(key, '') if hasattr(value, 'get') else ''

@register.filter
def stage_name(value):
    return {'unquoted':'未记录报价','quoted':'报价未注册','registered':'注册未首交','trade':'已有首交'}.get(value,value)

@register.filter
def identity_name(value):
    return {'unknown':'新客待核实','new':'已确认新客','existing':'历史已交易客户'}.get(value,value)

@register.filter
def dt(value):
    if not value:return '未记录'
    return timestamp(value).strftime('%Y-%m-%d %H:%M')
