let config=null, dailyPlan=null, selected=0, screenImage=null, selection=null, dragStart=null, runStatus=null;
const $=id=>document.getElementById(id);
async function api(path,method='GET',data){const response=await fetch('/api/'+path,{method,headers:method==='GET'?{}:{'Content-Type':'application/json'},body:method==='GET'?undefined:JSON.stringify(data||{})});const result=await response.json();if(!response.ok)throw new Error(result.error||'请求失败');return result}
function toast(message,error=false){const box=$('toast');box.textContent=message;box.className='toast'+(error?' error':'');box.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>box.style.display='none',4500)}
function protect(fn){return async(...args)=>{try{await fn(...args)}catch(error){toast(error.message,true)}}}
function tab(name){
  const section=$(name);
  if(!section?.classList.contains('section'))return;
  document.querySelectorAll('.section').forEach(x=>x.classList.toggle('active',x===section));
  document.querySelectorAll('.nav button').forEach(x=>x.classList.toggle('active',x.dataset.tab===name));
  document.querySelector('.workspace').scrollTop=0;
  history.replaceState(null,'','#'+name);
}
function confirmAction(message){
  const dialog=$('confirm-dialog');
  $('confirm-message').textContent=message;
  dialog.returnValue='cancel';
  return new Promise(resolve=>{
    dialog.onclose=()=>resolve(dialog.returnValue==='ok');
    dialog.showModal();
  });
}
document.querySelectorAll('.nav button').forEach(button=>button.onclick=()=>tab(button.dataset.tab));
if(location.hash)tab(location.hash.slice(1));
function loadFields(){const e=config.emulator,r=config.runtime||{},n=config.notifications||{},m=n.email||{};
  $('close-tool-after-run').checked = r.close_tool_after_run === true;
  $('adb-path').value=e.adb_path;$('serial').value=e.serial;$('emulator-path').value=e.executable||'';$('package').value=e.package;$('emulator-args').value=(e.arguments||[]).join('\n');$('launch-delay').value=e.launch_delay??10;$('stop-emulator').checked=!!e.stop_after_run;$('image-timeout').value=r.image_timeout??12;$('stop-on-error').checked=!!r.stop_on_error;$('daily-reset-at').value=r.daily_reset_at||'00:00';
  $('notify-success').checked=n.on_success!==false;$('notify-error').checked=n.on_error!==false;$('webhooks').value=(n.webhooks||[]).map(x=>x.url).join('\n');$('email-enabled').checked=!!m.enabled;$('email-provider').value=m.provider||(m.qq_number||!m.host?'qq':'smtp');$('qq-number').value=m.qq_number||'';$('smtp-auth-code').value='';$('smtp-auth-code').placeholder=m.has_authorization_code?'已保存，留空保留':'';$('smtp-host').value=m.host||'';$('smtp-port').value=m.port||465;$('smtp-security').value=m.security||(m.port===465?'ssl':'starttls');$('smtp-user').value=m.username||'';$('smtp-sender').value=m.sender||'';$('smtp-recipient').value=m.recipient||'';$('smtp-env').value=m.password_env||'HKA_SMTP_PASSWORD';updateEmailFields();renderTasks();}
function updateEmailFields(){const qq=$('email-provider').value==='qq';$('qq-email-fields').hidden=!qq;$('custom-email-fields').hidden=qq;$('smtp-auth-code').closest('label').hidden=!qq}
$('email-provider').onchange=updateEmailFields;
function collectFields(){const e=config.emulator,r=config.runtime||(config.runtime={}),n=config.notifications||(config.notifications={}),m=n.email||(n.email={});
  r.close_tool_after_run = $('close-tool-after-run').checked;
  e.adb_path=$('adb-path').value.trim();e.serial=$('serial').value.trim();e.executable=$('emulator-path').value.trim();e.package=$('package').value.trim();e.arguments=$('emulator-args').value.split('\n').map(x=>x.trim()).filter(Boolean);e.launch_delay=Number($('launch-delay').value);e.stop_after_run=$('stop-emulator').checked;r.image_timeout=Number($('image-timeout').value);r.stop_on_error=$('stop-on-error').checked;r.scheduler_enabled=false;r.daily_reset_at=$('daily-reset-at').value;
  n.on_success=$('notify-success').checked;n.on_error=$('notify-error').checked;n.webhooks=$('webhooks').value.split('\n').map(x=>x.trim()).filter(Boolean).map(url=>(n.webhooks||[]).find(hook=>hook.url===url)||{url,enabled:true});m.enabled=$('email-enabled').checked;m.provider=$('email-provider').value;m.qq_number=$('qq-number').value.trim();m.authorization_code=$('smtp-auth-code').value.trim();m.host=$('smtp-host').value.trim();m.port=Number($('smtp-port').value);m.security=$('smtp-security').value;m.username=$('smtp-user').value.trim();m.sender=$('smtp-sender').value.trim();m.recipient=$('smtp-recipient').value.trim();m.password_env=$('smtp-env').value.trim();}
function applyTask(){if(!config.tasks.length)return;const t=config.tasks[selected];let schedule,steps;try{schedule=JSON.parse($('task-schedule').value);steps=JSON.parse($('task-steps').value)}catch(e){throw new Error('任务 JSON 格式错误: '+e.message)}t.id=$('task-id').value.trim();t.name=$('task-name').value.trim();t.kind=$('task-kind').value;t.schedule=schedule;t.steps=steps;renderTasks(false)}
function renderTasks(fill=true){
  const list=$('task-list');
  list.replaceChildren();
  config.tasks.forEach((task,index)=>{
    const row=document.createElement('div');
    row.className='task-row'+(index===selected?' current':'');
    const enabled=document.createElement('input');
    enabled.type='checkbox';
    enabled.checked=task.enabled;
    enabled.setAttribute('aria-label','启用 '+task.name);
    enabled.onchange=()=>{task.enabled=enabled.checked;showPlanSummary()};
    const edit=document.createElement('button');
    edit.className='task-item';
    edit.append((index+1)+'. '+task.name);
    const item=dailyPlan?.items.find(item=>item.id===task.id);
    if(item){
      const note=document.createElement('small');
      note.textContent=item.reason;
      edit.append(note);
    }
    edit.onclick=protect(()=>{
      applyTask();
      selected=index;
      renderTasks();
      if(matchMedia('(max-width:750px)').matches)$('task-editor').scrollIntoView({block:'start'});
    });
    const freq=document.createElement('select');
    freq.setAttribute('aria-label',task.name+' 执行频率');
    freq.add(new Option('每天只执行一次','once_per_day'));
    freq.add(new Option('每次运行都执行','every_run'));
    freq.value=task.frequency||'once_per_day';
    freq.onchange=()=>{task.frequency=freq.value};
    const remove=document.createElement('button');
    remove.className='remove-task';
    remove.textContent='删除';
    remove.setAttribute('aria-label','删除 '+task.name);
    remove.onclick=protect(()=>removeTask(index));
    row.append(enabled,edit,freq,remove);
    list.append(row);
  });
  if(!config.tasks.length){
    const empty=document.createElement('p');
    empty.className='task-empty';
    empty.textContent='暂无任务，点击「添加任务」开始。';
    list.append(empty);
  }
  if(fill)fillTask();
  showPlanSummary();
}
function fillTask(){
  const t=config.tasks[selected];
  $('task-editor').hidden=!t;
  if(!t)return;
  $('editor-title').textContent=t.name;
  $('task-id').value=t.id;
  $('task-name').value=t.name;
  $('task-kind').value=t.kind||'custom';
  $('task-schedule').value=JSON.stringify(t.schedule,null,2);
  $('task-steps').value=JSON.stringify(t.steps,null,2);
  $('move-up').disabled=selected===0;
  $('move-down').disabled=selected===config.tasks.length-1;
  renderStepList();
}
function readSteps(){const steps=JSON.parse($('task-steps').value);if(!Array.isArray(steps))throw new Error('步骤必须是 JSON 数组');return steps}
function setSteps(steps){$('task-steps').value=JSON.stringify(steps,null,2);renderStepList()}
const flowNames={claim_guild_daily:'领取社团每日奖励',claim_mail:'领取邮件附件',enter_game:'进入上次服务器',refresh_resonance:'共鸣屋免费刷新',free_gift:'零时馈礼',quick_battle:'快捷战斗',stamina_farm:'体力本刷取',expeditions:'使魔探险',claim_presence:'存在感奖励',claim_weekly:'每周奖励',bonus_double_stamina:'双倍体力额外 BONUS'};
function stepLabel(step){const names={launch_game:'启动游戏',wait_image:'等待图像',tap_image:'点击图像',tap:'点击坐标',swipe:'滑动',key:'按键',sleep:'等待',screenshot:'截图',repeat:'重复',if_image:'条件',game_flow:'游戏流程'};return (names[step.type]||step.type)+' · '+((step.flow?(flowNames[step.flow]||step.flow)+(step.options?.stage?' · '+step.options.stage:''):'')||step.image||step.name||((step.x!==undefined)?step.x+','+step.y:'')||(step.seconds!==undefined?step.seconds+' 秒':'')||(step.times!==undefined?step.times+' 次':''))}
function renderStepList(){let steps;try{steps=readSteps()}catch(e){$('step-list').textContent='JSON 格式错误，无法显示步骤';return}const list=$('step-list');list.textContent='';steps.forEach((step,i)=>{const row=document.createElement('div');row.className='row';row.style.cssText='border:1px solid var(--line);border-radius:8px;padding:6px 9px';const label=document.createElement('span');label.textContent=(i+1)+'. '+stepLabel(step);label.style.flex='1';row.append(label);[['↑',-1],['↓',1],['×',0]].forEach(([text,delta])=>{const button=document.createElement('button');button.textContent=text;button.style.padding='3px 8px';button.onclick=()=>{try{const items=readSteps();if(delta===0)items.splice(i,1);else{const target=i+delta;if(target<0||target>=items.length)return;[items[i],items[target]]=[items[target],items[i]]}setSteps(items)}catch(e){toast(e.message,true)}};row.append(button)});list.append(row)});if(!steps.length)list.textContent='暂无步骤';renderFlowOptions()}
function normalizeWeapons(c){
  const positions=[[1255,790],[1480,790],[1480,565]],oldPositions=[[1105,644],[1255,570],[1040,790]];
  const weapons=Object.fromEntries(['A','B','C'].map((slot,i)=>[slot,{x:positions[i][0],y:positions[i][1],mode:'off',...(c.weapons?.[slot]||{})}]));
  if(!c.weapons){
    (c.skills||[]).forEach((skill,i)=>{if(i>2)return;Object.assign(weapons['ABC'[i]],skill);if(skill.x===oldPositions[i][0]&&skill.y===oldPositions[i][1])Object.assign(weapons['ABC'[i]],{x:positions[i][0],y:positions[i][1]})});
    if(!Object.values(weapons).some(w=>w.mode!=='off'))Object.assign(weapons.C,c.shoot||{mode:'tap'});
  }
  c.weapons=weapons;delete c.skills;delete c.shoot;return weapons;
}
function renderFlowOptions(){
  const box=$('flow-options');box.textContent='';let steps;try{steps=readSteps()}catch(e){return}
  const save=()=>{$('task-steps').value=JSON.stringify(steps,null,2)};
  steps.forEach((step,index)=>{
    if(step.type!=='game_flow'||!['stamina_farm','bonus_double_stamina'].includes(step.flow))return;
    const o=step.options||(step.options={}),c=o.combat||(o.combat={}),weapons=normalizeWeapons(c);
    const panel=document.createElement('div');panel.className='card';box.append(panel);
    const title=document.createElement('h3');title.textContent='步骤 '+(index+1)+' · '+flowNames[step.flow];panel.append(title);
    const row=document.createElement('div');row.className='row';panel.append(row);
    function field(parent,label,kind,value,change){
      const wrap=document.createElement('label');wrap.textContent=label;
      const input=document.createElement('input');input.type=kind;
      if(kind==='checkbox')input.checked=!!value;else input.value=value;
      input.onchange=()=>{change(kind==='checkbox'?input.checked:Number(input.value));save()};wrap.append(input);parent.append(wrap);return input;
    }
    if(step.flow==='stamina_farm' && config.tasks[selected].id!=='stamina_farm_after_presence'){
      const runLimit=field(row,'本阶段最多刷取（0 为不限）','number',o.max_runs??20,v=>o.max_runs=v);
      runLimit.min=0;
      runLimit.max=30;
    }
    const stopHint=document.createElement('p');stopHint.className='hint';stopHint.textContent='每场按普通体力用券。100 点起用双倍券，50–99 点普通刷取，低于 50 点停止。'+(step.flow==='bonus_double_stamina'?'本项要求双倍体力至少 50 点，每次执行最多一场。':config.tasks[selected].id==='stamina_farm_after_presence'?'本项不限场次。':'');panel.append(stopHint);
    const modeWrap=document.createElement('label');modeWrap.textContent='战斗方式';const battleMode=document.createElement('select');
    [['move_shoot','移动并执行 A/B/C'],['buttons','原地执行 A/B/C（自动套）']].forEach(([value,label])=>battleMode.add(new Option(label,value)));
    battleMode.value=c.mode||'move_shoot';battleMode.onchange=()=>{c.mode=battleMode.value;save()};modeWrap.append(battleMode);row.append(modeWrap);
    const duration=field(row,'每次长按（毫秒）','number',c.hold_ms??900,v=>c.hold_ms=v);duration.min=100;duration.max=20000;
    const interval=field(row,'每轮移动/等待（毫秒）','number',c.movement_ms??6500,v=>c.movement_ms=v);interval.min=100;interval.max=20000;
    const hint=document.createElement('p');hint.className='hint';hint.textContent='A/B/C 对应装备页从左到右的武器槽位，坐标基准为 1600×900。';panel.append(hint);
    const controls=document.createElement('div');controls.className='grid3';panel.append(controls);
    ['A','B','C'].forEach((slot,i)=>{
      const item=weapons[slot],group=document.createElement('div'),wrap=document.createElement('label');wrap.textContent='射击 '+slot+' · '+['左下','右下','右上'][i]+'武器键';
      const select=document.createElement('select');Object.entries({off:'关闭',tap:'循环点按',hold:'循环长按',once:'仅进图一次'}).forEach(([value,label])=>select.add(new Option(label,value)));
      select.value=item.mode;select.onchange=()=>{item.mode=select.value;save()};wrap.append(select);group.append(wrap);
      const coords=document.createElement('details'),summary=document.createElement('summary');summary.textContent='校准坐标';coords.append(summary);
      ['x','y'].forEach(axis=>{const input=field(coords,axis.toUpperCase(),'number',item[axis],v=>item[axis]=v);input.min=0;input.max=axis==='x'?1600:900});group.append(coords);controls.append(group);
    });
  });
  save();
}
$('new-game-flow').add(new Option('签到、连登与生日奖励','claim_login_rewards'),1);
$('new-game-flow').add(new Option('领取邮件附件','claim_mail'),2);
$('new-game-flow').add(new Option('领取社团每日奖励','claim_guild_daily'),3);
$('new-step-type').onchange=()=>{const game=$('new-step-type').value==='game_flow';$('new-game-flow').hidden=!game;$('new-step-image').hidden=game};
function addStep(){const kind=$('new-step-type').value,value=kind==='game_flow'?$('new-game-flow').value:$('new-step-image').value.trim();let step;if(['tap_image','wait_image','if_image','repeat','game_flow'].includes(kind)&&!value)throw new Error('请填写流程或模板名');if(kind==='game_flow'){const [flow,stage]=value.split(':');step={type:kind,flow,options:stage?{stage}:{}}}else if(kind==='tap_image')step={type:kind,image:value,timeout:8,wait:1};else if(kind==='wait_image')step={type:kind,image:value,timeout:20};else if(kind==='if_image')step={type:kind,image:value,then:[],else:[]};else if(kind==='repeat')step={type:kind,times:10,steps:[{type:'tap_image',image:value,timeout:2,on_missing:'break_repeat',wait:1}]};else if(kind==='tap'){const parts=value.split(',').map(Number);if(parts.length!==2||parts.some(x=>!Number.isFinite(x)||x<0||x>1))throw new Error('坐标请输入 0 到 1 之间的 x,y');step={type:kind,x:parts[0],y:parts[1]}}else if(kind==='sleep'){const seconds=Number(value);if(!Number.isFinite(seconds)||seconds<0||seconds>3600)throw new Error('请输入 0 到 3600 秒');step={type:kind,seconds}}else if(kind==='key')step={type:kind,code:4};else if(kind==='launch_game')step={type:kind,wait:20};else step={type:'screenshot',name:'manual'};const steps=readSteps();steps.push(step);setSteps(steps);$('new-step-image').value=''}
function showPlanSummary(){
  if(!config||!dailyPlan)return;
  const enabled=config.tasks.filter(t=>t.enabled).length;
  const pending=dailyPlan.items.filter(t=>t.status==='pending').length;
  $('daily-plan').textContent=dailyPlan.date+' · 已启用 '+enabled+' / '+config.tasks.length+' 项 · 已保存计划待执行 '+pending+' 项';
}
async function save(){if($('smtp-auth-code').value.trim()&&typeof config.notifications?.email?.has_authorization_code!=='boolean')throw new Error('请重新启动助手后再保存 SMTP 授权码');applyTask();collectFields();await api('config','POST',config);removedTasks.length=0;$('undo-delete').hidden=true;config=await api('config');loadFields();await updatePlan();toast('配置已保存')}
async function doctor(){await save();const result=await api('doctor','POST',{});const text=result.serial+' · '+result.size.join('×')+' · 游戏'+(result.installed?'已安装':'未安装');$('doctor-result').textContent=text;toast(text,!result.installed)}
async function run(tasks=null,force=false){await save();await api('run','POST',{tasks,force});tab('logs');await updateStatus()}
async function resetToday(){
  const date=dailyPlan?.date;
  if(!date||!await confirmAction('重置 '+date+' 的执行记录？下次运行时可重新执行当天任务。'))return;
  const result=await api('reset-today','POST',{date});
  await updatePlan();
  toast(result.reset_count?'已重置 '+result.reset_count+' 项任务':'今天没有可重置的任务记录');
}
$('reset-today').onclick=withAction(resetToday);
let lastPlanRead=0, online=false, busy=false;
function updateControls(){
  const running=!!runStatus?.running;
  $('run-top').disabled=!online||running||busy;
  $('stop-top').disabled=!online||!running;
  $('reset-today').disabled=!online||running||busy||!dailyPlan;
  $('save-top').disabled=!online||running||busy;
  $('test-task').disabled=!online||running||busy;
}
async function updatePlan(){
  const plan=await api('plan');
  if(JSON.stringify(plan)!==JSON.stringify(dailyPlan)){
    dailyPlan=plan;
    renderTasks(false);
  }
  lastPlanRead=Date.now();
}
async function updateStatus(){
  const wasRunning=runStatus?.running;
  runStatus=await api('status');
  online=true;
  const result=runStatus.result;
  const attention=result?.attention_required;
  $('attention-alert').hidden=!attention;
  $('attention-alert').textContent=attention?.message||'';
  const labels={failed:'失败',success:'完成',cancelled:'已停止',partial:'部分完成'};
  $('run-badge').textContent=runStatus.running?'运行中':attention?'需要处理':labels[result?.status]||'待机';
  $('run-badge').className='badge '+(runStatus.running?'run':result?.status==='failed'?'bad':result?.status==='success'?'ok':'');
  const log=$('full-log'),content=(runStatus.logs||[]).join('\n')||'尚无日志';
  if(log.textContent!==content){
    const atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-8;
    log.textContent=content;
    if(atEnd)log.scrollTop=log.scrollHeight;
  }
  $('result-details').hidden=!result;
  $('result-json').textContent=result?JSON.stringify(result,null,2):'';
  updateControls();
  if(runStatus.running||wasRunning||Date.now()-lastPlanRead>=30000)await updatePlan();
}
async function poll(){
  try{await updateStatus()}catch(e){
    online=false;
    $('run-badge').textContent='连接中断 · 正在重试';
    $('run-badge').className='badge bad';
    updateControls();
  }finally{setTimeout(poll,2000)}
}
function withAction(action){
  return protect(async()=>{
    if(busy)return;
    busy=true;
    updateControls();
    try{await action()}finally{busy=false;updateControls()}
  });
}
$('test-email').onclick=protect(async()=>{const button=$('test-email');button.disabled=true;try{await save();await api('test-email','POST',{});toast('测试邮件已发送')}finally{button.disabled=false}});
let screenshotId=null, latestRefresh=0;
async function refreshScreen(){
  const request=++latestRefresh;
  const response=await fetch('/api/screenshot?time='+Date.now());
  if(!response.ok)throw new Error((await response.json()).error);
  const id=response.headers.get('X-Screenshot-Id');
  if(!id)throw new Error('请重新启动助手并刷新页面后获取截图');
  const url=URL.createObjectURL(await response.blob());
  try{
    const image=new Image();
    image.src=url;
    await image.decode();
    if(request!==latestRefresh)return;
    const canvas=$('screen');
    screenImage=image;
    screenshotId=id;
    selection=null;
    dragStart=null;
    canvas.width=image.naturalWidth;
    canvas.height=image.naturalHeight;
    canvas.hidden=false;
    $('screen-placeholder').hidden=true;
    $('selection-info').textContent='尚未框选区域';
    drawScreen();
    toast('截图已更新：'+canvas.width+'×'+canvas.height);
  }finally{URL.revokeObjectURL(url)}
}
function drawScreen(){if(!screenImage)return;const canvas=$('screen'),ctx=canvas.getContext('2d');ctx.drawImage(screenImage,0,0,canvas.width,canvas.height);if(selection){const [x1,y1,x2,y2]=selection;ctx.strokeStyle='#6be1e9';ctx.lineWidth=Math.max(2,canvas.width/600);ctx.strokeRect(x1*canvas.width,y1*canvas.height,(x2-x1)*canvas.width,(y2-y1)*canvas.height);ctx.fillStyle='#6be1e944';ctx.fillRect(x1*canvas.width,y1*canvas.height,(x2-x1)*canvas.width,(y2-y1)*canvas.height)}}
function point(event){const rect=$('screen').getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))]}
$('screen').onpointerdown=e=>{dragStart=point(e);$('screen').setPointerCapture(e.pointerId)};
$('screen').onpointermove=e=>{if(!dragStart||document.querySelector('input[name="screen-mode"]:checked').value!=='crop')return;const p=point(e);selection=[Math.min(dragStart[0],p[0]),Math.min(dragStart[1],p[1]),Math.max(dragStart[0],p[0]),Math.max(dragStart[1],p[1])];drawScreen()};
$('screen').onpointerup=protect(async(e)=>{if(!dragStart)return;const start=dragStart;dragStart=null;const p=point(e);if(document.querySelector('input[name="screen-mode"]:checked').value==='tap'){await api('tap','POST',{x:p[0],y:p[1]});if($('record-tap').checked&&config.tasks[selected]){applyTask();config.tasks[selected].steps.push({type:'tap',x:Number(p[0].toFixed(4)),y:Number(p[1].toFixed(4)),wait:0.5});fillTask();toast('已点击，并记录到 '+config.tasks[selected].name)}else toast('已点击模拟器');setTimeout(()=>refreshScreen().catch(e=>toast(e.message,true)),600);return}selection=[Math.min(start[0],p[0]),Math.min(start[1],p[1]),Math.max(start[0],p[0]),Math.max(start[1],p[1])];$('selection-info').textContent='已框选 '+Math.round((selection[2]-selection[0])*$('screen').width)+'×'+Math.round((selection[3]-selection[1])*$('screen').height)+' 像素';drawScreen()});
async function listTemplates(){const files=await api('templates');const box=$('template-list'),options=$('template-options');box.textContent='';options.textContent='';files.forEach(name=>{const item=document.createElement('span');item.textContent=name;box.append(item);const option=document.createElement('option');option.value=name;options.append(option)});if(!files.length)box.textContent='暂无模板'}
async function saveTemplate(){
  if(!selection||!screenshotId)throw new Error('请先在截图上框选按钮');
  let name=$('template-name').value.trim();
  if(!name.endsWith('.png'))name+='.png';
  const result=await api('template','POST',{name,box:selection,screenshot_id:screenshotId});
  toast('已保存 '+result.name+' · '+result.size.join('×'));
  await listTemplates();
}
$('save-top').onclick=withAction(save);
$('doctor').onclick=withAction(doctor);
$('run-top').onclick=withAction(()=>run());
$('stop-top').onclick=protect(async()=>{await api('stop','POST',{});toast('正在停止任务');await updateStatus()});
$('test-task').onclick=withAction(()=>run([$('task-id').value.trim()],true));
$('refresh-screen').onclick=protect(refreshScreen);
$('save-template').onclick=protect(saveTemplate);
let presets=null;
const removedTasks=[];
function addToList(task,index){
  config.tasks.splice(index,0,task);
  selected=index;
  renderTasks();
  $('task-picker').close();
  toast('已添加「'+task.name+'」，保存后生效');
}
$('add-task').onclick=protect(async()=>{
  applyTask();
  if(!presets)presets=await api('task-presets');
  const list=$('preset-list');
  list.replaceChildren();
  const order=presets.map(t=>t.id);
  for(const preset of presets){
    const button=document.createElement('button');
    button.dataset.id=preset.id;
    button.disabled=config.tasks.some(t=>t.id===preset.id);
    button.textContent=preset.name+(button.disabled?' · 已添加':preset.id==='festival_event'?' · 待配置':'');
    button.onclick=()=>{
      const task=JSON.parse(JSON.stringify(preset));
      task.enabled=preset.id!=='festival_event';
      const rank=order.indexOf(task.id);
      const next=config.tasks.findIndex(t=>order.indexOf(t.id)>rank);
      addToList(task,next<0?config.tasks.length:next);
    };
    list.append(button);
  }
  $('custom-name').value='';
  $('task-picker').showModal();
});
$('close-picker').onclick=()=>$('task-picker').close();
$('custom-form').onsubmit=e=>{
  e.preventDefault();
  const name=$('custom-name').value.trim();
  if(!name){$('custom-name').focus();return}
  let id='custom_'+Date.now().toString(36);
  while(config.tasks.some(t=>t.id===id))id+='_1';
  addToList({id,name,kind:'custom',enabled:true,frequency:'once_per_day',
             schedule:{type:'daily'},steps:[]},config.tasks.length);
};
async function removeTask(index){
  const task=config.tasks[index],current=config.tasks[selected];
  if(!await confirmAction('删除「'+task.name+'」？保存后生效。'))return;
  applyTask();
  removedTasks.push({index,task});
  config.tasks.splice(index,1);
  selected=current===task?Math.max(0,Math.min(index,config.tasks.length-1)):config.tasks.indexOf(current);
  $('undo-delete').hidden=false;
  renderTasks();
}
$('undo-delete').onclick=protect(()=>{
  applyTask();
  const item=removedTasks[removedTasks.length-1];
  if(!item)return;
  if(config.tasks.some(t=>t.id===item.task.id))throw new Error('此任务 ID 已存在');
  removedTasks.pop();
  selected=Math.min(item.index,config.tasks.length);
  config.tasks.splice(selected,0,item.task);
  $('undo-delete').hidden=!removedTasks.length;
  renderTasks();
});
$('add-step').onclick=protect(async()=>addStep());$('task-steps').oninput=()=>renderStepList();
$('move-up').onclick=protect(async()=>{applyTask();if(selected>0){[config.tasks[selected-1],config.tasks[selected]]=[config.tasks[selected],config.tasks[selected-1]];selected--;renderTasks()}});
$('move-down').onclick=protect(async()=>{applyTask();if(selected<config.tasks.length-1){[config.tasks[selected+1],config.tasks[selected]]=[config.tasks[selected],config.tasks[selected+1]];selected++;renderTasks()}});
(async()=>{
  try{
    config=await api('config');
    loadFields();
    await Promise.all([updatePlan(),listTemplates()]);
    await poll();
  }catch(e){toast(e.message,true)}
})();
