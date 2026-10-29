
function toast(message){const el=document.getElementById('toast');el.textContent=message;el.style.display='block';setTimeout(()=>el.style.display='none',5000)}
let dirty=false;
document.querySelectorAll('form[data-fetch]').forEach(form=>{
 form.addEventListener('input',()=>{dirty=true});
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(!form.reportValidity())return;
  const error=form.querySelector('.form-error');const buttons=[...form.querySelectorAll('button')];
  if(error)error.textContent='';buttons.forEach(b=>b.disabled=true);
  try{const response=await fetch(form.getAttribute('action'),{method:'POST',body:new FormData(form),headers:{Accept:'application/json'}});const type=response.headers.get('Content-Type')||'';
   if(!type.includes('application/json'))throw new Error('请求未完成，可能登录已失效。输入已保留，请重新登录后重试。');
   const result=await response.json();if(!response.ok||!result.ok){const conflict=response.status===409?' 服务器当前版本为 '+(result.current_version||'已更新')+'；本页输入已保留，请另开页面核对最新记录后再提交，不会自动覆盖。':'';throw new Error((result.error||'操作失败，请重试。')+conflict)}
   dirty=false;if(result.redirect){location.href=result.redirect}else{toast(result.message||'已保存')}
  }catch(exc){if(error)error.textContent=exc.message;else toast(exc.message)}finally{buttons.forEach(b=>b.disabled=false)}
 });
});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue=''}});
document.querySelectorAll('[data-account]').forEach(button=>button.addEventListener('click',()=>{const form=document.getElementById('account-form');for(const [key,value] of Object.entries({username:button.dataset.account,name:button.dataset.name,role:button.dataset.role,departments:button.dataset.departments,version:button.dataset.version})){form.elements[key].value=value}form.elements.password.value='';form.scrollIntoView({block:'center'});dirty=false}));

document.querySelectorAll('input[name=excluded]').forEach(input=>input.addEventListener('change',()=>{const excluded=[...document.querySelectorAll('input[name=excluded]:checked')].filter(x=>x.dataset.inRange==='1').length;const valid=document.getElementById('preview-valid');if(valid)valid.textContent=Number(valid.dataset.original)-excluded;const summary=document.getElementById('preview-excluded');if(summary)summary.textContent=excluded}));

(() => {
 const panel=document.getElementById('crm-panel');
 if(!panel)return;
 const get=id=>document.getElementById(id);
 const controls=get('crm-controls');
 const buttons=[...panel.querySelectorAll('[data-crm-action]')];
 const retry=get('crm-status-retry');
 let current=null;
 let submitting=false;
 let statusUnavailable=false;
 let polling=null;
 let readingStatus=false;

 function updateButtons(){
  const disabled=submitting||statusUnavailable||Boolean(current&&current.busy);
  if(controls)controls.hidden=Boolean(current&&!current.can_manage);
  buttons.forEach(button=>{
   button.disabled=disabled||Boolean(current&&!current.can_manage);
   if(button.dataset.crmAction==='refresh')button.textContent=submitting?'正在提交…':current&&current.busy?'读取中…':'更新数据';
  });
  panel.setAttribute('aria-busy',String(submitting||Boolean(current&&current.busy)));
 }

 function showError(message){
  get('crm-status-error-text').textContent=message;
  get('crm-status-error').hidden=false;
 }

 function addText(parent,tag,value,className){
  const node=document.createElement(tag);
  node.textContent=value;
  if(className)node.className=className;
  parent.append(node);
  return node;
 }

 function render(crm){
  current=crm;
  get('crm-state').textContent=crm.label;
  get('crm-state').className='badge'+(['not_configured','auth_expired','failed','partial','interrupted'].includes(crm.state)?' warn':'');
  get('crm-feedback').textContent=crm.message;
  get('crm-feedback').className='notice'+(['auth_expired','failed','interrupted'].includes(crm.state)?' error':'');
  const snapshot=crm.last_success;
  get('crm-no-snapshot').hidden=Boolean(snapshot);
  get('crm-snapshot').hidden=!snapshot;
  get('crm-snapshot-state').textContent=snapshot?(snapshot.is_historical?'历史快照':'有效快照'):'暂无快照';
  const diagnosis=get('crm-diagnosis');
  diagnosis.removeAttribute('href');
  if(snapshot){
   for(const [id,value] of Object.entries({'crm-snapshot-name':snapshot.name,'crm-read-at':snapshot.read_at,'crm-cutoff':snapshot.cutoff,'crm-coverage':snapshot.coverage,'crm-count':snapshot.count,'crm-version':snapshot.version,'crm-source':snapshot.source||'授权范围内来源','crm-precision':snapshot.time_precision==='day'?'日期精度':snapshot.time_precision==='timestamp'?'含时区时刻':'以源截止为准','crm-snapshot-type':snapshot.type==='cases'?'经营案例':'表格快照'}))get(id).textContent=value;
   get('crm-history-note').hidden=!snapshot.is_historical;
   const validLink=typeof snapshot.diagnosis_url==='string'&&/^\/diagnosis\/\?(?:batch|snapshot)=\d+$/.test(snapshot.diagnosis_url);
   diagnosis.hidden=!validLink;
   if(validLink)diagnosis.setAttribute('href',snapshot.diagnosis_url);
  }
  const history=get('crm-runs');
  history.replaceChildren();
  for(const run of crm.runs){
   const row=document.createElement('tr');
   const time=document.createElement('td');
   addText(time,'span',run.started_at||'尚未开始');
   time.append(document.createElement('br'));
   addText(time,'small',run.completed_at||'尚未完成');
   const kind=document.createElement('td');
   addText(kind,'span',run.kind==='check'?'检查连接':'更新数据');
   if(run.requester){kind.append(document.createElement('br'));addText(kind,'small',run.requester)}
   const result=document.createElement('td');
   addText(result,'span',run.label,'badge');
   addText(result,'p',run.message,'crm-run-message');
   addText(result,'small','记录编号 '+run.id+(run.reused?' · 复用原快照':''));
   if(run.kind==='refresh'&&run.type){result.append(document.createElement('br'));addText(result,'small',run.type==='cases'?'经营案例':'表格快照')}
   if(typeof run.diagnosis_url==='string'&&/^\/diagnosis\/\?(?:batch|snapshot)=\d+$/.test(run.diagnosis_url)){
    result.append(document.createElement('br'));
    addText(result,'a','查看对应快照 →').setAttribute('href',run.diagnosis_url);
   }
   const scope=document.createElement('td');
   if(run.cutoff){addText(scope,'span',run.cutoff);scope.append(document.createElement('br'))}
   if(run.coverage){addText(scope,'span',run.coverage);scope.append(document.createElement('br'))}
   if(run.count!==null&&run.count!==undefined)addText(scope,'span',run.count+' 条');
   else addText(scope,'small',run.kind==='check'?'未读取业务记录':'未发布新的可见快照');
   row.append(time,kind,result,scope);
   history.append(row);
  }
  if(!crm.runs.length){const row=document.createElement('tr');const cell=addText(row,'td','尚无检查或更新记录。');cell.colSpan=4;history.append(row)}
  updateButtons();
 }

 async function request(url,options={}){
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),20000);
  try{
   const response=await fetch(url,{...options,cache:'no-store',signal:controller.signal,headers:{Accept:'application/json'}});
   if(!(response.headers.get('Content-Type')||'').includes('application/json'))throw new Error('invalid_response');
   const result=await response.json();
   if(!response.ok||!result.ok)throw new Error(response.status===403?'forbidden':'request_failed');
   return result;
  }finally{clearTimeout(timeout)}
 }

 function schedule(){
  clearTimeout(polling);
  polling=null;
  if(current&&current.busy&&!statusUnavailable)polling=setTimeout(()=>readStatus(),2000);
 }

 async function readStatus(clearError=true){
  if(readingStatus)return;
  readingStatus=true;
  retry.disabled=true;
  try{
   const result=await request('/api/crm/status/');
   if(!result.crm||!Array.isArray(result.crm.runs))throw new Error('invalid_response');
   statusUnavailable=false;
   render(result.crm);
   if(clearError)get('crm-status-error').hidden=true;
   schedule();
  }catch(exc){
   statusUnavailable=true;
   clearTimeout(polling);
   showError('暂时无法读取更新状态，可能是网络或登录状态变化。这不代表后台读取任务失败；已显示的快照保留，请重试读取状态。');
  }finally{
   readingStatus=false;
   retry.disabled=false;
   updateButtons();
  }
 }

 buttons.forEach(button=>button.addEventListener('click',async()=>{
  if(submitting||statusUnavailable||current&&current.busy)return;
  submitting=true;
  clearTimeout(polling);
  get('crm-status-error').hidden=true;
  updateButtons();
  let submitted=false;
  try{
   await request(button.dataset.crmAction==='check'?'/api/crm/check/':'/api/crm/refresh/',{method:'POST',body:new FormData(controls)});
   submitted=true;
  }catch(exc){
   showError(exc.message==='forbidden'?'当前账号不能执行此操作，请检查登录和账号权限。':'未能确认本次操作是否提交，请先核对更新记录。后台任务状态将单独读取，不会将通信失败记成取数失败。');
  }finally{
   submitting=false;
   await readStatus(submitted);
   updateButtons();
  }
 }));
 retry.addEventListener('click',()=>readStatus());
 try{
  const initial=JSON.parse(get('crm-initial-state').textContent);
  if(!initial||!Array.isArray(initial.runs))throw new Error('invalid_state');
  render(initial);
  schedule();
 }catch(exc){
  statusUnavailable=true;
  showError('暂时无法读取CRM入口状态，已显示的数据保留。请重试读取状态。');
  updateButtons();
 }
})();

document.querySelectorAll('form[data-resource-request]').forEach(form=>{
 const requested=form.elements.requested_on;
 const first=form.elements.first_review_on;
 const second=form.elements.second_review_on;
 function suggest(dateInput,days){
  if(!requested.value||dateInput.value&&!dateInput.dataset.suggested)return;
  const date=new Date(requested.value+'T00:00:00Z');
  if(Number.isNaN(date.getTime()))return;
  date.setUTCDate(date.getUTCDate()+days);
  dateInput.value=date.toISOString().slice(0,10);
  dateInput.dataset.suggested='true';
 }
 requested.addEventListener('change',()=>{suggest(first,14);suggest(second,28)});
 [first,second].forEach(input=>input.addEventListener('input',()=>{delete input.dataset.suggested}));
});

document.querySelectorAll('form[data-resource-event]').forEach(form=>{
 const kind=form.elements.kind;
 function updateFields(){
  const isReview=['review1','review2'].includes(kind.value);
  form.querySelectorAll('[data-event-field]').forEach(label=>{
   const visible=label.dataset.eventField==='review'?isReview:isReview||kind.value==='obstacle';
   label.hidden=!visible;
   const input=label.querySelector('input,textarea,select');
   input.disabled=!visible;
   input.required=visible;
  });
 }
 kind.addEventListener('change',updateFields);
 updateFields();
});
