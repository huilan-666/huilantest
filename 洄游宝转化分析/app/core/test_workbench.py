import copy
import io
import json
import sys
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import OperationalError
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from . import crm, workbench as wb
from .models import BusinessCase, CaseAssessment, CaseSnapshot, CRMReadRun, Profile, ResourceEvent, ResourceRequest


def historical():
    return json.loads((Path(__file__).parent / 'data' / 'historical_cases_20261028.json').read_text(encoding='utf-8'))


def source_payload():
    payload = historical()
    payload.update(source='crm-controlled-cases', source_label='受控经营案例验证', coverage='业务一部、业务二部及未知部门的受控案例')
    payload['cases'] = [payload['cases'][0], payload['cases'][7], payload['cases'][8]]
    payload['cases'][0]['department'] = '业务一部'
    payload['cases'][1]['department'] = '业务二部'
    return payload


class WorkbenchTests(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = override_settings(MEDIA_ROOT=Path(self.directory.name), CRM_PROVIDER='wb_protocol_provider')
        self.settings.enable(); self.addCleanup(self.settings.disable)
        self.users = {}
        for name, role, departments in [('运营蕙兰', 'ops', []), ('一部负责人', 'business', ['业务一部']),
                                         ('二部负责人', 'business', ['业务二部']), ('公司负责人', 'ceo', []), ('全权蕙兰', 'ops', [])]:
            user = get_user_model().objects.create(username=name, first_name=name)
            Profile.objects.create(user=user, role=role, departments=departments)
            if name == '全权蕙兰':
                user.is_superuser = True; user.save(update_fields=['is_superuser'])
            self.users[name] = user
        self.client.force_login(self.users['全权蕙兰'])
        self.launcher = patch('core.crm.launch_run'); self.launcher.start(); self.addCleanup(self.launcher.stop)
        provider = types.ModuleType('wb_protocol_provider')
        provider.health = Mock(return_value={'status': 'connected'})
        provider.fetch_snapshot = Mock(return_value=source_payload())
        self.provider = provider
        self.modules = patch.dict(sys.modules, {'wb_protocol_provider': provider}); self.modules.start(); self.addCleanup(self.modules.stop)

    def post(self, url, values, status=200):
        result = self.client.post(url, json.dumps(values, ensure_ascii=False), content_type='application/json')
        self.assertEqual(result.status_code, status, result.content[:1000])
        return result

    def publish(self, payload=None):
        return wb.publish_cases(payload or source_payload())[0]

    def case(self, key='qiyu'):
        return BusinessCase.objects.get(source_key=key)

    def assessment(self, case=None, **values):
        case = case or self.case()
        case.refresh_from_db()
        data = {'case_version': case.version, 'snapshot': case.current_snapshot_id, 'target': 'blocker', 'category': 'price',
                'verdict': 'confirmed', 'evidence': '客户亲口确认当前费用异议，保留沟通记录', 'occurred_on': '2026-10-28', **values}
        return self.post(f'/api/cases/{case.pk}/assessments/', data)

    def request(self, case=None, **values):
        case = case or self.case(); case.refresh_from_db()
        data = {'case_version': case.version, 'snapshot': case.current_snapshot_id, 'problem': '客户希望明确费用和方案依据',
                'resource_type': 'expert', 'owner': '一部负责人', 'requested_on': '2026-10-28',
                'next_step': '安排针对具体问题的专家沟通', 'expected_feedback': 'KP确认可接受条件与测试计划',
                'first_review_on': '2026-11-11', 'second_review_on': '2026-11-25', **values}
        self.post(f'/api/cases/{case.pk}/resources/', data)
        return ResourceRequest.objects.latest('pk')

    def event(self, resource, kind='intervention', **values):
        resource.refresh_from_db()
        data = {'resource_version': resource.version, 'kind': kind, 'occurred_on': '2026-10-28',
                'actor': '一部负责人', 'evidence': '已完成具体方案说明，客户反馈另行记录', **values}
        return self.post(f'/api/resources/{resource.pk}/events/', data)

    def detail(self, case=None, snapshot=None):
        case = case or self.case()
        url = f'/api/cases/{case.pk}/' + (f'?snapshot={snapshot.pk}' if snapshot else '')
        result = self.client.get(url)
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()['case_detail']

    def test_historical_initialization_order_real_boundaries_and_legacy_routes(self):
        output = io.StringIO()
        call_command('load_business_cases', stdout=output)
        self.assertEqual((CaseSnapshot.objects.count(), BusinessCase.objects.count()), (1, 10))
        result = self.client.get('/api/workbench/').json()['workbench']
        self.assertEqual(result['counts'], {'total': 10, 'pending': 9, 'completed': 1, 'migration': 0, 'uncovered': 0})
        self.assertEqual([c['source_key'] for c in result['cases']], ['qiyu','jc','kaishi','zhancheng','xinding','oufuli','koudai','anlan','wendang','sinofloc'])
        self.assertEqual(result['source_context']['observed_on'], '2026-10-28')
        self.assertEqual(result['source_context']['source_cutoff'], '')
        self.assertEqual(result['source_context']['time_precision'], 'day')
        self.assertEqual((ResourceRequest.objects.count(), CaseAssessment.objects.count()), (0, 0))
        self.assessment()
        self.request()
        call_command('load_business_cases', stdout=output)
        self.assertEqual((CaseSnapshot.objects.count(), ResourceRequest.objects.count(), CaseAssessment.objects.count()), (1, 1, 1))
        self.assertEqual(self.client.get('/diagnosis/').status_code, 200)
        self.assertEqual(self.client.get('/cases/' + str(self.case().pk) + '/').status_code, 200)
        for user, expected in [('运营蕙兰', 'ops'), ('一部负责人', 'business'), ('公司负责人', 'ceo')]:
            self.client.force_login(self.users[user])
            self.assertEqual(self.client.get('/api/workbench/').json()['workbench']['view'], expected)

    def test_source_update_preserves_local_history_absent_cases_and_namespaces(self):
        first = self.publish()
        case = self.case(); self.assessment(case); resource = self.request(case)
        data = source_payload(); data['observed_on'] = '2026-10-29'; data['cases'][0]['blockers'] = [{'code':'contract','detail':'合同待法务确认','evidence':'客户合同沟通'}]
        data['cases'] = data['cases'][:1]
        second = self.publish(data)
        case.refresh_from_db()
        current = self.detail(case)
        self.assertEqual(case.current_snapshot_id, second.pk)
        self.assertTrue(current['case']['source_changed'])
        self.assertTrue(current['assessments'][0]['stale'])
        self.assertFalse(any(b['code'] == 'price' and b['verdict'] == 'confirmed' for b in current['blockers']))
        old = self.detail(case, first)
        self.assertTrue(any(b['code'] == 'price' and b['verdict'] == 'confirmed' for b in old['blockers']))
        result = self.client.get('/api/workbench/?snapshot=' + str(second.pk)).json()['workbench']
        self.assertEqual((result['counts']['total'], result['counts']['pending'], result['counts']['uncovered']), (3, 1, 2))
        self.assertFalse(self.detail(self.case('anlan'))['case']['is_covered'])
        self.assertEqual(ResourceRequest.objects.get(pk=resource.pk).case_id, case.pk)
        other = source_payload(); other['source'] = 'another-namespace'
        self.publish(other)
        self.assertEqual(BusinessCase.objects.filter(source_key='qiyu').count(), 2)
        same, created = wb.publish_cases(data)
        self.assertEqual((same.pk, created), (second.pk, False))

    def test_invalid_sources_roll_back_and_keep_latest(self):
        first = self.publish()
        invalid = []
        a = source_payload(); a['cases'].append(copy.deepcopy(a['cases'][0])); invalid.append(a)
        a = source_payload(); a['cases'][0]['source_key'] = ''; invalid.append(a)
        a = source_payload(); a['observed_on'] = '2026-10-27'; invalid.append(a)
        a = source_payload(); a['cases'][0]['phase'] = '未知枚举'; invalid.append(a)
        a = source_payload(); a['cases'][0].update(outcome='crm_traded',outcome_on='2027-01-01',outcome_evidence='未来计划被错填结果'); invalid.append(a)
        for value in invalid:
            with self.assertRaises(wb.DomainError):
                self.publish(value)
        for complete, cases in [(False, source_payload()['cases']), (True, [])]:
            a = source_payload(); a['complete'], a['cases'] = complete, cases
            self.assertEqual(wb.publish_cases(a), (None, False))
        a = source_payload(); a['observed_on'] = '2026-10-29'; a['cases'][0]['enterprise'] = '新来源名称'
        with patch('core.workbench.BusinessCase.objects.create', side_effect=OperationalError('模拟不可输出token')):
            a['cases'].append({**copy.deepcopy(a['cases'][0]), 'source_key':'new-business-case'})
            with self.assertRaises(OperationalError):
                self.publish(a)
        self.assertEqual(CaseSnapshot.objects.count(), 1)
        self.assertEqual(self.case().data['enterprise'], first.payload['cases'][0]['enterprise'])

    def test_filters_specific_blockers_unknown_and_registered_advice(self):
        self.publish()
        result = self.client.get('/api/workbench/?stage=registered').json()['workbench']
        self.assertEqual(result['counts']['total'], 2)
        self.assertTrue(all(c['phase'] == 'registered' for c in result['cases']))
        price = self.client.get('/api/workbench/?reason=price&q=奇域').json()['workbench']
        self.assertEqual(len(price['cases']), 1)
        self.assertEqual(price['blocker_groups'][0]['code'], 'price')
        self.assessment(verdict='rejected')
        result = self.client.get('/api/workbench/?reason=price').json()['workbench']
        self.assertEqual(result['blocker_groups'][0]['rejected'], 1)
        self.assertEqual(result['blocker_groups'][0]['confirmed'], 0)
        from .services import stage_advice
        from types import SimpleNamespace
        record = SimpleNamespace(data={'created':'2026-10-01','registered':'2026-10-05'}, identity='unknown', batch=SimpleNamespace(config={'cutoff':'2026-10-28'}))
        advice = stage_advice(record)
        self.assertNotIn('资料', str(advice)); self.assertNotIn('KYC', str(advice))

    def test_workday_boundaries_calendar_and_frozen_source_date(self):
        item = {'phase':'registered','outcome':'pending','registered_on':'2026-10-12','registered_evidence':'真实注册日志','migration':'natural'}
        for cutoff, expected, level in [('2026-10-15',3,'normal'),('2026-10-16',4,'check'),('2026-10-19',5,'check'),('2026-10-20',6,'priority')]:
            actual = wb.waiting_status(item, date.fromisoformat(cutoff))
            self.assertEqual((actual['days'], actual['level']), (expected, level))
        self.assertEqual(wb.waiting_status({**item,'registered_on':'2026-09-30'},date(2026,10,10))['days'], 3)
        self.assertEqual(wb.waiting_status({**item,'registered_on':'2026-09-24'},date(2026,9,28))['days'], 1)
        self.assertEqual(wb.waiting_status({**item,'registered_on':'2026-09-19'},date(2026,9,20))['days'], 1)
        for changed in [{'registered_evidence':''},{'registered_on':'2026-10-29'},{'registered_on':'2025-10-12'}]:
            self.assertIsNone(wb.waiting_status({**item,**changed},date(2026,10,20))['days'])
        for changed in [{'phase':'prequote'},{'outcome':'crm_traded'},{'migration':'excluded'}]:
            self.assertEqual(wb.waiting_status({**item,**changed},date(2026,10,20))['level'], 'not_applicable')

    def test_kp_plan_priority_and_outcome_semantic_validation(self):
        self.publish()
        case = self.case()
        good_kp = {'identity':'boss','identity_evidence':'老板亲口确认自己拍板','phone':'yes','wechat':'yes','reachable':'yes','willing':'yes'}
        for key in ['phone','wechat','reachable','willing']:
            values = {**good_kp, key:'unknown'}
            self.post(f'/api/cases/{case.pk}/assessments/', {'case_version':case.version,'target':'kp','verdict':'confirmed','evidence':'客户反馈','occurred_on':'2026-10-28','value':values},400)
        self.assessment(target='kp', value=good_kp)
        self.assertTrue(self.detail()['kp']['valid'])
        plan = {'date_text':'11月初','amount_text':'3万','currency':'人民币','purpose':'首笔测试','kp_confirmation':'kp_said'}
        self.assessment(target='plan',value=plan)
        self.assertTrue(self.detail()['plan']['valid'])
        case.refresh_from_db()
        self.post(f'/api/cases/{case.pk}/assessments/', {'case_version':case.version,'target':'plan','verdict':'confirmed','evidence':'缺币种','occurred_on':'2026-10-28','value':{**plan,'currency':''}},400)
        self.assessment(target='priority',value={'priority_group':'future','priority_reason':'客户未来资金但需再挖场景','future_quote':'客户称下个月有资金','future_on':'2026-10-28','next_review_on':'2026-11-02'})
        self.assertEqual(self.detail()['case']['future_quote'], '客户称下个月有资金')
        self.assessment(target='outcome', value={'outcome':'crm_traded','outcome_on':'2026-10-28'})
        self.assertTrue(self.detail()['case']['is_completed'])
        case.refresh_from_db()
        self.post(f'/api/cases/{case.pk}/assessments/', {'case_version':case.version,'target':'outcome','verdict':'confirmed','evidence':'只有销售声称交易','occurred_on':'2026-10-28','outcome':'first_trade','outcome_on':'2026-10-28'},400)
        self.post(f'/api/cases/{case.pk}/assessments/', {'case_version':case.version,'target':'priority','verdict':'confirmed','evidence':'非法混入结果','occurred_on':'2026-10-28','value':{'priority_group':'future','priority_reason':'说明','outcome':'first_trade'}},400)

    def test_cas_and_transaction_failures_leave_no_partial_local_records(self):
        self.publish(); case = self.case(); initial = case.version
        self.assessment()
        self.post(f'/api/cases/{case.pk}/assessments/', {'case_version':initial,'target':'blocker','category':'price','verdict':'confirmed','evidence':'过期提交','occurred_on':'2026-10-28'},409)
        self.assertEqual(CaseAssessment.objects.count(), 1)
        case.refresh_from_db(); old = case.version
        with patch('core.workbench.CaseAssessment.objects.create', side_effect=OperationalError('password=不得输出')):
            with self.assertLogs('core.views',level='ERROR') as logs:
                result = self.client.post(f'/api/cases/{case.pk}/assessments/',json.dumps({'case_version':old,'target':'blocker','category':'price','verdict':'confirmed','evidence':'事务失败','occurred_on':'2026-10-28'}),content_type='application/json')
        self.assertEqual(result.status_code,503)
        self.assertNotIn('不得输出',str(logs.output)+result.content.decode())
        case.refresh_from_db(); self.assertEqual(case.version,old)
        resource = self.request()
        case.refresh_from_db()
        self.post(f'/api/cases/{case.pk}/resources/', {'case_version':old,'problem':'重复','resource_type':'expert','owner':'一部负责人','requested_on':'2026-10-28','next_step':'沟通','expected_feedback':'反馈','first_review_on':'2026-11-11','second_review_on':'2026-11-25'},409)
        self.assertEqual(ResourceRequest.objects.count(),1)
        version = resource.version
        self.event(resource)
        self.post(f'/api/resources/{resource.pk}/events/', {'resource_version':version,'kind':'feedback','occurred_on':'2026-10-28','actor':'一部负责人','evidence':'过期'},409)
        self.assertEqual(ResourceEvent.objects.count(),1)
        resource.refresh_from_db(); current_version=resource.version
        with patch('core.workbench.ResourceEvent.objects.create',side_effect=OperationalError('token=不得泄露的事件保存错误')):
            with self.assertLogs('core.views',level='ERROR'):
                self.post(f'/api/resources/{resource.pk}/events/',{'resource_version':current_version,'kind':'feedback','occurred_on':'2026-10-28','actor':'负责人','evidence':'事务回滚验证'},503)
        resource.refresh_from_db()
        self.assertEqual(resource.version,current_version)
        self.assertEqual(ResourceEvent.objects.count(),1)

    def test_resources_independent_steps_reviews_and_ceo_counts(self):
        self.publish(); one = self.request(); two = self.request()
        result = self.client.get('/api/workbench/?view=ceo').json()['workbench']['resource_summary']
        self.assertEqual((result['requested_count'],result['intervention_count'],result['case_count']),(2,0,0))
        self.event(one); self.event(two)
        self.event(one,'feedback',evidence='KP已理解解释但还需内部确认')
        self.event(one,'obstacle',obstacle_status='partial',evidence='部分疑虑已解释，签约仍待核实')
        for kind in ['review1','review2']:
            self.event(one,kind,obstacle_status='partial',execution='已完成一次专家说明',customer_feedback='客户仍需确认合同',result_progress='尚未报价，不能判断转化改善')
        detail = self.detail()
        self.assertEqual(len(detail['resources']),2)
        self.assertEqual(sum(review['done'] for r in detail['resources'] for review in r['reviews']),2)
        result = self.client.get('/api/workbench/?view=ceo').json()['workbench']['resource_summary']
        self.assertEqual((result['intervention_count'],result['case_count']),(2,1))
        self.assertFalse(result['chains'][0]['comparison']['comparable'])
        self.event(two,'cancel',evidence='客户暂缓，取消本次安排但保留实际介入历史')
        self.assertEqual(ResourceEvent.objects.filter(resource=two).count(),2)
        one.refresh_from_db()
        for occurred in ['2026-10-27',(timezone.localdate()+timedelta(days=1)).isoformat()]:
            self.post(f'/api/resources/{one.pk}/events/',{'resource_version':one.version,'kind':'feedback','occurred_on':occurred,'actor':'负责人','evidence':'非法日期'},400)
        anlan=self.case('anlan')
        prior_resource=self.request(anlan)
        self.event(prior_resource)
        before=self.client.get('/api/workbench/').json()['workbench']['counts']['pending']
        self.assessment(anlan,target='outcome',evidence='已核对资金转入及成功下发订单，属于本案首次成功交易',
                        value={'outcome':'first_trade','outcome_on':'2026-10-28','fund_received':'yes','disbursed':'yes',
                               'association_evidence':'本案来源标识anlan关联的交易主体与订单已核实'})
        detail=self.detail(anlan)
        self.assertEqual(detail['case']['phase'],'registered')
        self.assertEqual(detail['case']['outcome'],'first_trade')
        self.assertTrue(detail['case']['is_completed'])
        self.assertEqual(detail['source_case']['outcome'],'pending')
        self.assertEqual(detail['resources'][0]['id'],prior_resource.pk)
        result=self.client.get('/api/workbench/?view=ceo').json()['workbench']
        self.assertEqual(result['counts']['pending'],before-1)
        self.assertFalse(any(c['id']==anlan.pk for group in result['priority_groups'] for c in group['cases']))
        chain=next(chain for chain in result['resource_summary']['chains'] if chain['case']['id']==anlan.pk)
        self.assertEqual(chain['case']['outcome'],'first_trade')
        self.assertEqual(chain['resources'][0]['id'],prior_resource.pk)
        self.assertTrue(chain['resources'][0]['has_intervention'])
        self.assertFalse(chain['comparison']['comparable'])

    def test_current_department_authorization_applies_to_all_history_and_writes(self):
        first = self.publish(); case = self.case(); request = self.request()
        for name in ['运营蕙兰','公司负责人','一部负责人','二部负责人']:
            self.client.force_login(self.users[name])
            for view in ['ops','business','ceo']:
                result = self.client.get('/api/workbench/?view='+view).json()['workbench']
                self.assertEqual(result['counts']['total'],3 if name in ['运营蕙兰','公司负责人'] else 1)
            if name in ['运营蕙兰','公司负责人']:
                self.post(f'/api/cases/{case.pk}/resources/',{},403)
            if name == '运营蕙兰':
                self.post(f'/api/cases/{case.pk}/assessments/',{'target':'blocker','category':'price','verdict':'confirmed'},403)
            if name == '一部负责人':
                self.assertEqual(self.client.get('/api/cases/'+str(self.case('wendang').pk)+'/').status_code,404)
                self.post(f'/api/cases/{case.pk}/department/',{'case_version':case.version,'department':'业务二部','evidence':'越权'},403)
        self.client.force_login(self.users['运营蕙兰']); case.refresh_from_db()
        self.post(f'/api/cases/{case.pk}/department/',{'case_version':case.version,'department':'业务二部','evidence':'运营已核实新部门归属'})
        self.client.force_login(self.users['一部负责人'])
        self.assertEqual(self.client.get(f'/api/cases/{case.pk}/?snapshot={first.pk}').status_code,404)
        self.post(f'/api/resources/{request.pk}/events/',{'resource_version':request.version,'kind':'feedback','occurred_on':'2026-10-28','actor':'原负责人','evidence':'已越权'},404)
        self.assertEqual(self.client.get('/api/workbench/').json()['workbench']['counts']['total'],0)
        self.client.force_login(self.users['全权蕙兰'])
        updated = source_payload(); updated['observed_on']='2026-10-29'; updated['cases'][0]['department']=''
        self.publish(updated)
        case.refresh_from_db(); self.assertEqual(case.department,'业务二部')
        self.client.logout(); self.assertEqual(self.client.get('/api/workbench/').status_code,401)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.users['全权蕙兰'])
        self.assertEqual(csrf.post(f'/api/cases/{case.pk}/assessments/',{}).status_code,403)

    def test_event_permission_is_rechecked_if_assignment_changes_during_validation(self):
        self.publish(); case = self.case(); resource = self.request()
        self.client.force_login(self.users['一部负责人'])
        original = wb._version
        changed = False
        def change_department(value):
            nonlocal changed
            if not changed:
                changed = True
                case.refresh_from_db()
                wb.assign_department(self.users['运营蕙兰'], case.pk, {'case_version':case.version,'department':'业务二部','evidence':'交错发生的真实归属核实'})
            return original(value)
        with patch('core.workbench._version', side_effect=change_department):
            self.post(f'/api/resources/{resource.pk}/events/',{'resource_version':resource.version,'kind':'feedback','occurred_on':'2026-10-28','actor':'原负责人','evidence':'归属变更后不能写入'},404)
        self.assertEqual(ResourceEvent.objects.count(),0)

    def test_script_text_remains_text_in_ssr_and_source_changes_do_not_apply_old_kp_plan_outcome(self):
        value=source_payload()
        script='</script><script>window.恶意执行=true</script>'
        value['cases'][0]['enterprise']=script
        value['cases'][0]['evidence'][0]['quote']=script
        first=self.publish(value)
        case=self.case()
        self.assessment(target='kp',value={'identity':'boss','identity_evidence':'可拍板的老板','phone':'yes','wechat':'yes','reachable':'yes','willing':'yes'})
        self.assessment(target='plan',value={'date_text':'11月初','amount_text':'3万','currency':'人民币','purpose':'首测','kp_confirmation':'kp_confirmed'})
        self.assessment(target='outcome',value={'outcome':'crm_traded','outcome_on':'2026-10-28'})
        page=self.client.get(f'/cases/{case.pk}/?snapshot={first.pk}')
        self.assertEqual(page.status_code,200)
        self.assertNotIn(script,page.content.decode())
        self.assertIn('&lt;script&gt;',page.content.decode())
        self.assertTrue(self.detail()['case']['is_completed'])
        value['observed_on']='2026-10-29'
        self.publish(value)
        current=self.detail()
        self.assertFalse(current['kp']['valid'])
        self.assertFalse(current['plan']['valid'])
        self.assertFalse(current['case']['is_completed'])
        self.assertTrue(all(a['stale'] for a in current['assessments']))
        old=self.detail(snapshot=first)
        self.assertTrue(old['kp']['valid']); self.assertTrue(old['plan']['valid']); self.assertTrue(old['case']['is_completed'])

    def test_rejected_source_outcome_and_priority_do_not_remain_effective_facts(self):
        payload=source_payload()
        payload['cases'][0].update(outcome='crm_traded',outcome_on='2026-10-28',outcome_evidence='来源CRM状态观察，待本地复查')
        self.publish(payload)
        self.assertTrue(self.detail()['case']['is_completed'])
        self.assessment(target='outcome',verdict='rejected',value={'outcome':'crm_traded','outcome_on':'2026-10-28'},evidence='核对发现该交易属于另一商机，否定本案观察')
        detail=self.detail()
        self.assertFalse(detail['case']['is_completed'])
        self.assertEqual(detail['case']['outcome'],'unknown')
        self.assertEqual(detail['source_case']['outcome'],'crm_traded')
        self.assessment(target='priority',verdict='rejected',value={'priority_group':'resource','priority_reason':'原排序依据不足'})
        self.assertEqual(self.detail()['case']['priority_group'],'unassigned')

    def test_crm_cases_branch_precision_links_empty_and_table_compatibility(self):
        def execute():
            run_id=self.client.post('/api/crm/refresh/').json()['run_id']; crm.execute_run(run_id)
            return CRMReadRun.objects.get(pk=run_id)
        run=execute(); self.assertEqual(run.status,'success'); self.assertIsNotNone(run.case_snapshot_id); self.assertIsNone(run.batch_id)
        self.assertIsNone(run.source_cutoff)
        summary=self.client.get('/api/crm/status/').json()['crm']
        self.assertEqual(summary['last_success']['type'],'cases')
        self.assertEqual(summary['last_success']['time_precision'],'day')
        self.assertEqual(summary['last_success']['diagnosis_url'],f'/diagnosis/?snapshot={run.case_snapshot_id}')
        self.assertIn('日期精度',summary['last_success']['cutoff'])
        duplicate=execute(); self.assertTrue(duplicate.reused); self.assertEqual(CaseSnapshot.objects.count(),1)
        for complete,cases,status in [(False,source_payload()['cases'],'partial'),(True,[],'success_empty')]:
            data=source_payload(); data['complete'],data['cases']=complete,cases; self.provider.fetch_snapshot.return_value=data
            actual=execute(); self.assertEqual(actual.status,status); self.assertEqual(actual.data_type,'cases')
            self.assertIsNone(actual.source_cutoff)
            self.assertEqual(actual.observed_on,date(2026,10,28))
            self.assertEqual(CaseSnapshot.objects.count(),1)
            self.assertEqual(self.client.get('/api/crm/status/').json()['crm']['last_success']['case_snapshot_id'],run.case_snapshot_id)
        from .test_crm import snapshot
        self.provider.fetch_snapshot.return_value=snapshot()
        table=execute(); self.assertEqual(table.status,'success'); self.assertIsNotNone(table.batch_id); self.assertIsNone(table.case_snapshot_id)
        self.assertEqual(self.client.get('/diagnosis/?batch='+str(table.batch_id)).status_code,200)
