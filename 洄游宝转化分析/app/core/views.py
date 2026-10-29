import json
import logging
import uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path

from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Max
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Action, Audit, Batch, Experience, ImportPreview, Profile, Reason, Record, Review
from .crm import context_status, start_run
from .policy import department_allowed, profile, require, scoped
from .services import FIELD_NAMES, DomainError, config_values, counts, create_preview, date_value, normalized, publish, read_file, record_ready, report, stage_advice, timestamp

User = get_user_model()
STATES = {'pending':'待执行','running':'进行中','review':'待复盘','done':'已复盘','cancelled':'已取消'}
EVIDENCE = {'unverified':'未验证经验','observation':'历史观察','verified':'已验证策略'}
PUBLICATION = {'draft':'草稿','review':'待业务确认','published':'已发布'}
logger = logging.getLogger(__name__)


def payload(request):
    if request.content_type == 'application/json':
        try:
            value = json.loads(request.body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError) as exc:
            raise DomainError('请求内容必须为JSON对象。') from exc
    return request.POST


def api(request):
    return request.path.startswith('/api/') or request.headers.get('Accept') == 'application/json'


def error_response(request, message, status=400, code='invalid'):
    if api(request) or request.method == 'POST':
        return JsonResponse({'ok':False,'error':message,'code':code},status=status)
    return render(request,'error.html',{'title':'操作未完成','error':message,'page_id':'','status':status},status=status)


def guarded(view):
    @wraps(view)
    def wrapper(request,*args,**kwargs):
        if not request.user.is_authenticated:
            return error_response(request,'请先登录。',401,'unauthenticated') if api(request) or request.method=='POST' else redirect('/login/')
        try:
            profile(request.user)
            return view(request,*args,**kwargs)
        except DomainError as exc:
            return error_response(request,str(exc),exc.status,exc.code)
        except PermissionDenied:
            return error_response(request,'当前角色没有此操作权限。',403,'forbidden')
        except Http404:
            return error_response(request,'记录不存在或不在授权范围内。',404,'not_found')
        except DatabaseError:
            request_id=uuid.uuid4().hex
            logger.error('数据库操作失败 request_id=%s user_id=%s',request_id,request.user.pk)
            return error_response(request,'数据保存或读取未完成，请保留输入后重试。请求号：'+request_id,503,'storage_error')
    return wrapper


def identity_context(request):
    if not request.user.is_authenticated:
        return {}
    try:p=profile(request.user)
    except PermissionDenied:return {}
    query=request.GET.copy()
    query.pop('page',None)
    admin=request.user.is_superuser
    return {'actor':p,'actor_name':request.user.first_name or request.user.username,'role_name':'全权限管理员' if admin else p.get_role_display(),'role_scope':'全公司' if admin or p.role!='business' else '、'.join(p.departments),'is_ops':admin or p.role=='ops','is_business':admin or p.role=='business','is_ceo':not admin and p.role=='ceo','page_query':query.urlencode()}


def page(request,name,title,pid,**context):
    return render(request,name,{'title':title,'page_id':pid,**context})


def login_page(request):
    error=''
    if request.method=='POST':
        username=request.POST.get('username','')
        user=authenticate(request,username=username,password=request.POST.get('password',''))
        if user and Profile.objects.filter(user=user).exists():
            login(request,user)
            return redirect('/diagnosis/')
        error='账号或密码无效，或账号尚未配置业务权限。'
    return render(request,'login.html',{'error':error,'title':'登录','page_id':'PAGE-F01-01','username':request.POST.get('username','')})


@guarded
@require_POST
def logout_view(request):
    logout(request)
    return redirect('/login/')


@guarded
def accounts(request):
    require(request.user,'ops')
    return page(request,'accounts.html','账号与部门','PAGE-F01-02',accounts=Profile.objects.select_related('user').order_by('user__username'))


@guarded
@require_POST
def save_account(request):
    require(request.user,'ops')
    d=payload(request)
    name=str(d.get('username','')).strip()
    role=d.get('role')
    if not name or role not in ['ops','business','ceo']:
        raise DomainError('请填写账号并选择有效角色。')
    departments=[x.strip() for x in str(d.get('departments','')).replace('，',',').split(',') if x.strip()]
    if role=='business' and not departments:
        raise DomainError('业务负责人必须指定授权部门。')
    password=d.get('password','')
    existing=User.objects.filter(username=name).first()
    if existing and existing.is_superuser and not request.user.is_superuser:
        raise PermissionDenied
    if existing==request.user and role!='ops':
        raise DomainError('不能通过此操作修改自己的运营管理角色。')
    if not existing and not password:
        raise DomainError('新账号必须设置初始密码。')
    if password:
        try:validate_password(password,existing)
        except ValidationError as exc:raise DomainError('；'.join(exc.messages)) from exc
    try:version=int(d.get('version') or 0)
    except (ValueError,TypeError) as exc:raise DomainError('账号版本无效。') from exc
    with transaction.atomic():
        if existing:
            p=get_object_or_404(Profile,user=existing)
            if p.version!=version:
                raise DomainError('账号已被修改，请刷新后核对。','conflict',409)
            if not Profile.objects.filter(pk=p.pk,version=version).update(role=role,departments=departments,version=version+1):
                raise DomainError('账号版本冲突，请刷新。','conflict',409)
            u=existing
            u.first_name=str(d.get('name','')).strip()
            if password:u.set_password(password)
            u.save()
        else:
            u=User.objects.create_user(username=name,password=password,first_name=str(d.get('name','')).strip())
            Profile.objects.create(user=u,role=role,departments=departments)
        Audit.objects.create(author=request.user,entity='account',entity_id=u.pk,event='save',version=version+1,detail={'role':role,'departments':departments})
    return JsonResponse({'ok':True,'message':'账号授权已保存；下一次请求按新授权判定。','redirect':'/accounts/'})


@guarded
def imports(request):
    p=profile(request.user)
    preview=None;rows=[];stats=None;preview_error=''
    if request.GET.get('preview'):
        require(request.user,'ops')
        preview=get_object_or_404(ImportPreview,pk=request.GET['preview'],owner=request.user)
        try:
            rows=normalized(preview,preview.mapping,preview.config)
            stats=counts(rows,preview.config)
            for row in rows:
                created=timestamp(row['data']['created'])
                row['display_in_range']=bool(created and date_value(preview.config['start'],'起日')<=created.date()<=date_value(preview.config['end'],'止日'))
        except DomainError as exc:preview_error=str(exc)
    batches=[]
    for b in Batch.objects.order_by('-pk')[:100]:
        count=visible_count(b,request.user)
        if p.role!='business' or count:
            batches.append({'batch':b,'count':count})
    return page(request,'imports.html','数据批次与导入','PAGE-F02-01',preview=preview,preview_error=preview_error,rows=rows[:1000],preview_truncated=len(rows)>1000,stats=stats,fields=FIELD_NAMES.items(),batches=batches,crm=context_status(request.user))


@guarded
def crm_status(request):
    return JsonResponse({'ok':True,'crm':context_status(request.user)})


@guarded
@require_POST
def crm_check(request):
    run=start_run(request.user,'check')
    return JsonResponse({'ok':True,'run_id':run.pk,'status_url':'/api/crm/status/'},status=202)


@guarded
@require_POST
def crm_refresh(request):
    run=start_run(request.user,'refresh')
    return JsonResponse({'ok':True,'run_id':run.pk,'status_url':'/api/crm/status/'},status=202)


def visible_count(b,user):
    return scoped(b.records.all(),user).count()


def batch_for(user,batch_id):
    b=get_object_or_404(Batch,pk=batch_id)
    if profile(user).role=='business' and not visible_count(b,user):raise Http404
    return b


@guarded
@require_POST
def upload(request):
    require(request.user,'ops')
    if 'file' not in request.FILES:raise DomainError('请选择文件。')
    p=create_preview(request.user,request.FILES['file'])
    return JsonResponse({'ok':True,'preview_id':p.pk,'redirect':f'/imports/?preview={p.pk}'})


@guarded
@require_POST
def remap(request,pk):
    require(request.user,'ops')
    p=get_object_or_404(ImportPreview,pk=pk,owner=request.user)
    d=payload(request)
    sheet=d.get('sheet',p.sheet)
    if sheet!=p.sheet:
        _,p.sheet,p.headers,p.raw_rows=read_file(Path(p.file_path).read_bytes(),p.filename,sheet)
    mapping={k:d.get('map_'+k,'') for k in FIELD_NAMES}
    config=config_values(d)
    normalized(p,mapping,config)
    p.mapping,p.config=mapping,config
    p.save()
    return JsonResponse({'ok':True,'redirect':f'/imports/?preview={p.pk}'})


@guarded
@require_POST
def publish_view(request,pk):
    require(request.user,'ops')
    p=get_object_or_404(ImportPreview,pk=pk,owner=request.user)
    d=payload(request)
    if str(d.get('confirmed','')).lower() not in ['true','1','on']:
        raise DomainError('请确认范围、截止日和剔除说明。')
    selected=d.getlist('excluded') if hasattr(d,'getlist') else d.get('excluded',[])
    exclusions={str(n):str(d.get('reason_'+str(n),'')).strip() for n in selected}
    batch,created=publish(p,request.user,p.mapping,p.config,exclusions)
    return JsonResponse({'ok':True,'created':created,'batch_id':batch.pk,'redirect':f'/diagnosis/?batch={batch.pk}'})


@guarded
@require_POST
def identity_review(request,pk):
    require(request.user,'ops')
    batch=batch_for(request.user,pk);d=payload(request)
    try:number=int(d.get('row'))
    except (ValueError,TypeError) as exc:raise DomainError('请选择原始行号。') from exc
    target=get_object_or_404(Record,batch=batch,row_number=number)
    identity=d.get('identity')
    evidence=str(d.get('evidence','')).strip()
    if identity not in ['new','existing','unknown'] or not evidence:raise DomainError('请选择身份并提供核实依据。')
    # 以源文件与旧快照规则产生新版，原始行和旧分析永不覆盖。
    _,sheet,headers,raw=read_file(Path(batch.source_path).read_bytes(),batch.name,batch.sheet)
    preview=ImportPreview(owner=request.user,filename=batch.name,file_hash=batch.file_hash,file_path=batch.source_path,sheet=sheet,headers=headers,raw_rows=raw,mapping=batch.mapping,config=batch.config)
    overrides={str(r.row_number):{'identity':r.identity,'unique_link':r.unique_link,'evidence':r.identity_evidence,'opportunity_id':r.opportunity_id,'customer_id':r.customer_id} for r in batch.records.all()}
    overrides[str(number)]={'identity':identity,'unique_link':str(d.get('unique_link','')).lower() in ['on','1','true'],'evidence':evidence,'opportunity_id':str(d.get('opportunity_id',target.opportunity_id)).strip(),'customer_id':str(d.get('customer_id',target.customer_id)).strip()}
    exclusions={str(r.row_number):r.exclusion_reason for r in batch.records.filter(excluded=True)}
    new,created=publish(preview,request.user,batch.mapping,batch.config,exclusions,overrides,previous=batch)
    return JsonResponse({'ok':True,'created':created,'batch_id':new.pk,'redirect':f'/diagnosis/?batch={new.pk}'})


def selected_batch(request):
    if request.GET.get('batch'):return batch_for(request.user,request.GET['batch'])
    for b in Batch.objects.order_by('-pk'):
        if profile(request.user).role!='business' or visible_count(b,request.user):return b
    return None


def make_report(request,batch):
    return report(batch,request.user,request.GET.get('window'),request.GET.get('group','stage'),request.GET.get('start'),request.GET.get('end'),request.GET.get('cutoff'))


@guarded
def diagnosis(request):
    if not request.GET.get('batch') and not request.path.startswith('/api/'):
        from .workbench_views import overview
        return overview(request)
    batch=selected_batch(request);data=make_report(request,batch) if batch else None
    if api(request):return JsonResponse({'ok':True,'report':data})
    batch_choices=[b for b in Batch.objects.order_by('-pk')[:100] if profile(request.user).role!='business' or visible_count(b,request.user)]
    if data:
        details=data['records'];stage=request.GET.get('stage','');q=request.GET.get('q','').strip()
        if stage:details=[r for r in details if r['stage']==stage]
        if q:details=[r for r in details if q in r['name'] or q in r['owner']]
        records_page=Paginator(details,20).get_page(request.GET.get('page',1))
    else:records_page=[]
    return page(request,'diagnosis.html','诊断总览','PAGE-F03-01',batch=batch,report=data,records_page=records_page,batches=batch_choices)


@guarded
def opportunity(request,pk):
    r=get_object_or_404(scoped(Record.objects.select_related('batch'),request.user),pk=pk)
    p=profile(request.user)
    advice=stage_advice(r)
    context={'record':r,'reasons':r.reasons.select_related('author').order_by('-pk'),'candidate':advice['candidate'],'advice':advice,'actions':scoped(Action.objects.filter(record=r),request.user).select_related('owner')}
    if api(request):
        return JsonResponse({'ok':True,'record':{'id':r.pk,'row':r.row_number,'data':r.data,'issues':r.issues,'identity':r.identity,'identity_evidence':r.identity_evidence,'unique_link':r.unique_link,'excluded':r.excluded,'exclusion_reason':r.exclusion_reason,'batch_id':r.batch_id,'batch_version':r.batch.version,'config':r.batch.config},'advice':advice,'reasons':[{'verdict':v.verdict,'evidence':v.evidence,'author':v.author.first_name,'time':v.created_at.isoformat()} for v in context['reasons']]})
    return page(request,'opportunity.html','商机详情','PAGE-F03-02',**context)


@guarded
@require_POST
def save_reason(request,pk):
    p=require(request.user,'ops','business')
    r=get_object_or_404(scoped(Record.objects.all(),request.user),pk=pk);d=payload(request)
    verdict=d.get('verdict','pending');evidence=str(d.get('evidence','')).strip()
    if verdict not in ['pending','confirmed','rejected'] or not evidence:raise DomainError('请选择结论并填写证据或说明。')
    if not request.user.is_superuser and p.role=='ops' and verdict!='pending':raise PermissionDenied
    with transaction.atomic():
        reason=Reason.objects.create(record=r,author=request.user,verdict=verdict,evidence=evidence)
        Audit.objects.create(author=request.user,entity='reason',entity_id=reason.pk,department=r.department,event=verdict)
    return JsonResponse({'ok':True,'redirect':f'/opportunities/{r.pk}/'})


def department_owners(department):
    return [p.user for p in Profile.objects.filter(user__is_active=True).select_related('user') if (p.user.is_superuser or p.role=='business') and department_allowed(p,department)]


@guarded
def actions(request):
    queryset=scoped(Action.objects.select_related('record','owner'),request.user).order_by('due_date','pk')
    if request.GET.get('status'):queryset=queryset.filter(status=request.GET['status'])
    if request.GET.get('q'):queryset=queryset.filter(task__icontains=request.GET['q'])
    if api(request):return JsonResponse({'ok':True,'actions':[{'id':a.pk,'task':a.task,'department':a.department,'status':a.status,'version':a.version,'due_date':str(a.due_date)} for a in queryset]})
    return page(request,'actions.html','行动复盘列表','PAGE-F04-01',actions=Paginator(queryset,20).get_page(request.GET.get('page',1)),states=STATES)


@guarded
def action_detail(request,pk=None):
    a=get_object_or_404(scoped(Action.objects.select_related('record','record__batch','owner'),request.user),pk=pk) if pk else None
    r=a.record if a else get_object_or_404(scoped(Record.objects.select_related('batch'),request.user),pk=request.GET.get('record'))
    p=profile(request.user)
    if not a:require(request.user,'business')
    batch_choices=[b for b in Batch.objects.order_by('-pk') if p.role!='business' or visible_count(b,request.user)]
    histories=scoped(Audit.objects.filter(entity='action',entity_id=a.pk),request.user).order_by('-pk') if a else []
    return page(request,'action.html','行动详情与复盘','PAGE-F04-02',action=a,record=r,suggested_task=stage_advice(r)['task'],states=STATES,owners=department_owners(r.department),due_date=a.due_date if a else timezone.localdate()+timedelta(days=14),second_date=a.second_review_date if a else timezone.localdate()+timedelta(days=28),reviews=a.reviews.select_related('author').order_by('round','pk') if a else [],histories=histories,batches=batch_choices)


@guarded
@require_POST
def save_action(request,pk=None):
    require(request.user,'business');d=payload(request)
    a=get_object_or_404(scoped(Action.objects.all(),request.user),pk=pk) if pk else None
    r=a.record if a else get_object_or_404(scoped(Record.objects.all(),request.user),pk=d.get('record_id'))
    task=str(d.get('task','')).strip();status=d.get('status','pending');note=str(d.get('note','')).strip()
    due=date_value(d.get('due_date'),'复盘日期');second=date_value(d.get('second_review_date'),'第二轮复盘日期')
    if not task or status not in STATES or second<due:raise DomainError('请填写动作，选择有效状态，并确认第二轮不早于第一轮。')
    if not str(d.get('owner','')).isdigit():raise DomainError('请选择有效的部门负责人。')
    owner=get_object_or_404(User,pk=d.get('owner'),is_active=True)
    op=profile(owner)
    if not (owner.is_superuser or op.role=='business') or not department_allowed(op,r.department):raise DomainError('负责人必须为本部门有权限的业务负责人。')
    allowed={'pending':['pending','running','cancelled'],'running':['running','review','cancelled'],'review':['review','done','cancelled'],'done':['done'],'cancelled':['cancelled']}
    if a and status not in allowed[a.status]:raise DomainError('该状态迁移不合法；终态需要新建行动。')
    if not a and status!='pending':raise DomainError('新行动必须从待执行开始。')
    if status in ['done','cancelled'] and not note:raise DomainError('已复盘或取消必须填写结果/说明。')
    with transaction.atomic():
        if a:
            try:version=int(d.get('version'))
            except (ValueError,TypeError) as exc:raise DomainError('缺少有效版本。') from exc
            updated=Action.objects.filter(pk=a.pk,version=version).update(task=task,owner=owner,due_date=due,second_review_date=second,status=status,note=note,version=version+1,updated_at=timezone.now())
            if not updated:raise DomainError('行动已被他人修改，输入已保留，请刷新核对后再提交。','conflict',409)
            a.refresh_from_db()
        else:
            a=Action.objects.create(record=r,department=r.department,opportunity_id=r.opportunity_id,customer_id=r.customer_id,creator=request.user,owner=owner,task=task,due_date=due,second_review_date=second,status=status,note=note)
        Audit.objects.create(author=request.user,entity='action',entity_id=a.pk,department=r.department,event='save',version=a.version,detail={'task':a.task,'owner_id':a.owner_id,'due_date':str(a.due_date),'second_review_date':str(a.second_review_date),'status':a.status,'note':a.note,'record_id':r.pk})
    return JsonResponse({'ok':True,'action_id':a.pk,'version':a.version,'redirect':f'/actions/{a.pk}/'})


def comparison(action,user,before,after,window):
    if not action.opportunity_id or not action.customer_id:return {'comparable':False,'reason':'缺稳定商机ID或客户关联，不能跨批判断效果。'}
    if not before or not after:return {'comparable':False,'reason':'缺少前后快照或同龄基线。'}
    b=report(before,user,window);f=report(after,user,window)
    if b['mode']!='formal' or f['mode']!='formal':return {'comparable':False,'reason':'新客分母或唯一关联尚未确认，不能判断效果。'}
    b_records={r.opportunity_id:r for r in scoped(before.records.filter(excluded=False),user) if record_ready(r)}
    f_records={r.opportunity_id:r for r in scoped(after.records.filter(excluded=False),user) if record_ready(r)}
    if action.opportunity_id not in b_records or action.opportunity_id not in f_records:return {'comparable':False,'reason':'所选快照未覆盖该稳定商机。'}
    b_eligible={r['id'] for r in b['records'] if r['mature']}
    f_eligible={r['id'] for r in f['records'] if r['mature']}
    b_ids={r.opportunity_id for r in b_records.values() if r.pk in b_eligible}
    f_ids={r.opportunity_id for r in f_records.values() if r.pk in f_eligible}
    if not b_ids or b_ids!=f_ids:return {'comparable':False,'reason':'成熟群体或同龄关联不一致，不能将样本替换解释为效果。'}
    if any(b_records[k].customer_id!=f_records[k].customer_id or b_records[k].data['created']!=f_records[k].data['created'] or b_records[k].data.get('source')!=f_records[k].data.get('source') or b_records[k].data.get('scenario')!=f_records[k].data.get('scenario') for k in b_ids):return {'comparable':False,'reason':'客户、创建时间或主要客群属性不一致，需核对可比性。'}
    return {'comparable':True,'window':window,'before':{k:b[k] for k in ['numerator','denominator','rate','quoted','registered']},'after':{k:f[k] for k in ['numerator','denominator','rate','quoted','registered']},'limitation':'前后变化不证明因果；固定观察期下既有结果可能不变。'}


@guarded
@require_POST
def save_review(request,pk):
    require(request.user,'business');a=get_object_or_404(scoped(Action.objects.all(),request.user),pk=pk);d=payload(request)
    try:round_no=int(d.get('round'));window=int(d.get('window',30))
    except (ValueError,TypeError) as exc:raise DomainError('复盘轮次和观察窗口必须有效。') from exc
    execution=str(d.get('execution','')).strip();note=str(d.get('note','')).strip()
    if round_no not in [1,2] or not execution or not note or not 1<=window<=3650:raise DomainError('请选择第一/二轮，填写实际执行与复盘结果。')
    before=batch_for(request.user,d['baseline']) if d.get('baseline') else None
    after=batch_for(request.user,d['followup']) if d.get('followup') else None
    result=comparison(a,request.user,before,after,window)
    try:
        source_cutoff=timestamp(d.get('source_cutoff'))
        exported=timestamp(d.get('exported_at'))
        imported=timestamp(d.get('imported_at'))
        generated=timestamp(d.get('generated_at'))
    except DomainError as exc:raise DomainError('耗时记录日期无效。') from exc
    times=[v for v in [source_cutoff,exported,imported,generated] if v]
    if times!=sorted(times):raise DomainError('已填写的源截止、导出、导入和生成时刻需按时间先后排列。')
    nodes=[source_cutoff,exported,imported,generated]
    result['timings']={'source_to_export_hours':round((exported-source_cutoff).total_seconds()/3600,2) if source_cutoff and exported else None,'export_to_import_hours':round((imported-exported).total_seconds()/3600,2) if exported and imported else None,'import_to_generation_hours':round((generated-imported).total_seconds()/3600,2) if imported and generated else None,'total_hours':round((generated-source_cutoff).total_seconds()/3600,2) if all(nodes) else None}
    with transaction.atomic():
        review=Review.objects.create(action=a,author=request.user,round=round_no,note=note,execution=execution,baseline=before,followup=after,window=window,comparison=result,source_cutoff=source_cutoff,exported_at=exported,imported_at=imported,generated_at=generated)
        Audit.objects.create(author=request.user,entity='review',entity_id=review.pk,department=a.department,event='save',detail={'action_id':a.pk,'round':round_no,'comparison':result})
    return JsonResponse({'ok':True,'review_id':review.pk,'comparison':result,'redirect':f'/actions/{a.pk}/'})


@guarded
def experiences(request):
    qs=scoped(Experience.objects.all(),request.user).select_related('author','confirmed_by').order_by('-created_at')
    selected=get_object_or_404(qs,pk=request.GET['id']) if request.GET.get('id') else None
    if api(request):
        return JsonResponse({'ok':True,'experiences':[{'id':x.pk,'condition':x.condition,'department':x.department,'evidence':x.evidence,'cause':x.cause,'task':x.task,'version':x.version,'editor_version':x.editor_version,'publication':x.publication,'evidence_status':x.evidence_status} for x in qs]})
    action=get_object_or_404(scoped(Action.objects.all(),request.user),pk=request.GET['action']) if request.GET.get('action') else None
    departments=sorted(set(Record.objects.exclude(department='').values_list('department',flat=True))) if profile(request.user).role=='ops' else profile(request.user).departments
    return page(request,'experiences.html','经验列表与维护','PAGE-F05-01',experiences=qs,selected=selected,from_action=action,evidence_states=EVIDENCE,publications=PUBLICATION,departments=departments)


@guarded
@require_POST
def save_experience(request,pk=None):
    require(request.user,'ops');d=payload(request)
    old=get_object_or_404(Experience,pk=pk) if pk else None
    department=str(d.get('department','')).strip()
    required={k:str(d.get(k,'')).strip() for k in ['condition','cause','evidence','task']}
    if not all(required.values()) or not department:raise DomainError('适用条件、原因、证据、动作和部门都必须填写。')
    estate=d.get('evidence_status','unverified');pub=d.get('publication','draft')
    if estate not in EVIDENCE or pub not in ['draft','review']:raise DomainError('运营只能维护草稿或提交业务确认。')
    validation_design=str(d.get('validation_design','')).strip();validation_result=str(d.get('validation_result','')).strip()
    if estate=='verified' and (not validation_design or not validation_result):raise DomainError('已验证策略必须提供验证设计和结果；前后观察不能自动升级。')
    refs=d.getlist('references') if hasattr(d,'getlist') else d.get('references',[])
    records=[]
    for ref in refs:
        r=get_object_or_404(Record,pk=ref)
        if r.department!=department:raise DomainError('经验不能引用其他部门的明细证据。','cross_department',403)
        records.append(r)
    action=get_object_or_404(Action,pk=d['action']) if d.get('action') else old.action if old else None
    if action and action.department!=department:raise DomainError('关联行动必须属于经验适用部门。','cross_department',403)
    values={**required,'department':department,'result':str(d.get('result','')).strip(),'validation_design':validation_design,'validation_result':validation_result,'evidence_status':estate,'publication':pub,'action':action}
    with transaction.atomic():
        if old:
            try:version=int(d.get('editor_version'))
            except (ValueError,TypeError) as exc:raise DomainError('经验缺少有效版本。') from exc
            if old.editor_version!=version:raise DomainError('经验版本冲突，输入已保留。','conflict',409)
            if old.publication=='published':
                # 先占用旧版编辑号防止同一已发布版并发创建两个后继；正文不变。
                if not Experience.objects.filter(pk=old.pk,editor_version=version).update(editor_version=version+1):raise DomainError('经验版本冲突。','conflict',409)
                latest=Experience.objects.filter(root_id=old.root_id).aggregate(v=Max('version'))['v']
                experience=Experience.objects.create(root_id=old.root_id,previous=old,version=latest+1,author=request.user,**values)
            else:
                if not Experience.objects.filter(pk=old.pk,editor_version=version).update(**values,editor_version=version+1,updated_at=timezone.now()):raise DomainError('经验版本冲突。','conflict',409)
                experience=Experience.objects.get(pk=old.pk)
        else:experience=Experience.objects.create(root_id=uuid.uuid4(),author=request.user,**values)
        if not refs and old:records=list(old.references.all())
        if any(r.department!=department for r in records):raise DomainError('修改部门前须移除其他部门证据。','cross_department',403)
        experience.references.set(records)
        Audit.objects.create(author=request.user,entity='experience',entity_id=experience.pk,department=department,event='save',version=experience.editor_version,detail={'publication':experience.publication,'version':experience.version})
    return JsonResponse({'ok':True,'experience_id':experience.pk,'version':experience.version,'editor_version':experience.editor_version,'redirect':f'/experiences/?id={experience.pk}'})


@guarded
@require_POST
def confirm_experience(request,pk):
    require(request.user,'business');d=payload(request)
    x=get_object_or_404(scoped(Experience.objects.all(),request.user),pk=pk)
    try:version=int(d.get('editor_version'))
    except (ValueError,TypeError) as exc:raise DomainError('缺少经验版本。') from exc
    if x.publication!='review':raise DomainError('只有待业务确认的草稿可以确认或退回。')
    if x.evidence_status=='verified' and (not x.validation_design or not x.validation_result or d.get('validation_confirmed') not in ['on','true',True,'1']):raise DomainError('请核查验证设计及结果，并明确确认策略验证。')
    if any(r.department!=x.department for r in x.references.all()):raise DomainError('引用证据范围不一致。','cross_department',403)
    state='draft' if d.get('decision')=='reject' else 'published'
    with transaction.atomic():
        updated=Experience.objects.filter(pk=x.pk,editor_version=version).update(publication=state,confirmed_by=request.user if state=='published' else None,confirmed_at=timezone.now() if state=='published' else None,editor_version=version+1)
        if not updated:raise DomainError('经验已被修改，请刷新核对。','conflict',409)
        Audit.objects.create(author=request.user,entity='experience',entity_id=x.pk,department=x.department,event=state,version=version+1)
    return JsonResponse({'ok':True,'redirect':f'/experiences/?id={x.pk}'})
