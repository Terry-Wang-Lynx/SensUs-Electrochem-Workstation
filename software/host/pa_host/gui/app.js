const $ = (id) => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`界面资源版本不一致（缺少 ${id}），请刷新页面后重试`);
  return node;
};
const state = { measurement: null, calibration: {points: [], model: null, curve: null}, drift: null, schedule: null, settings: null, workflow: null, history: {entries: []}, devices: {devices: [], selected_device_id: null, busy: false}, sampleRole: 'calibration', method: 'it', chartWindowS: 300, chartWindowFixed: true, chartRunId: null, lastHandledRunId: null, measureControlInitialized: false, showRaw: true, calibrationDirty: false, validationDirty: false, settingsDirty: false, driftDirty: false, exiting: false };
const pages = {
  measure: ['实时测量', '180 秒 IT 检测与末 20 秒稳态分析'],
  calibrate: ['标定与漂移', '选择标定范围并管理过渡期 bias'],
  schedule: ['稳定化 / 自动', '无人值守的定时连续 IT 检测'],
  history: ['历史记录', '恢复完整工作区并继续标定或测试'],
  // 🔴 缺这一条会让整个切页 handler TypeError —— pages[view][0] 直接取下标
  debug: ['硬件 DEBUG', '运行时改采样参数 · 每次变更留审计 · 电流与电位同图']
};

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const body = await response.text();
  let data = {};
  try { data = body ? JSON.parse(body) : {}; } catch { data = {error: body || '服务返回了无效响应'}; }
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}
function post(path, body = {}) { return api(path, {method: 'POST', body: JSON.stringify(body)}); }
function toast(message) { const node = $('toast'); node.textContent = message; node.classList.add('show'); setTimeout(() => node.classList.remove('show'), 2600); }
function errorBox(id, error) {
  const message = error?.message || String(error);
  const node = document.getElementById(id);
  if (node) { node.textContent = message; node.hidden = false; }
  else { console.error(message); window.alert(message); }
}
function fmt(value, digits = 2) {
  if(value===null||value===undefined||value===''||!Number.isFinite(Number(value)))return '--';
  const number=Number(value),formatted=number.toFixed(digits);
  return Object.is(number,-0)&&!formatted.startsWith('-')?`-${formatted}`:formatted;
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

// ── 主机侧滤波 ─────────────────────────────────────────────────────────────
// 这组状态同时服务实时测量和 Debug。原始 data/current_nA 永远不改；图表
// 和实时读数按当前设置计算一份派生数组。滤波器只接受连续有效段，绝不跨
// 饱和点传播恢复瞬态。
const FILTER_DEFAULTS = {
  mode: 'display', lowpass_enabled: false, lowpass_cutoff_hz: 1,
  lowpass_auto: true, lowpass_order: 2
};
state.filter = {...FILTER_DEFAULTS}; state.filterDirty = false; state.filterAutoCache = null;
function loadShowRaw(){
  try{return localStorage.getItem('sensus.showRaw') !== 'false'}catch{return true}
}
function persistShowRaw(value){
  try{localStorage.setItem('sensus.showRaw',String(Boolean(value)))}catch{}
}
state.showRaw=loadShowRaw();
function loadCalibrationSeriesVisibility(name){
  try{return localStorage.getItem(`sensus.${name}`)!=='false'}catch{return true}
}
function persistCalibrationSeriesVisibility(name,value){
  try{localStorage.setItem(`sensus.${name}`,String(Boolean(value)))}catch{}
}
state.showCalibrationPoints=loadCalibrationSeriesVisibility('showCalibrationPoints');
state.showTestPoints=loadCalibrationSeriesVisibility('showTestPoints');
function filterPanels(){return [...document.querySelectorAll('[data-filter-panel]')]}
function filterBoolean(value){return value === true || value === 'true' || value === 1 || value === '1'}
function readFilterPanel(panel){
  const out={...state.filter};
  panel.querySelectorAll('[data-filter-key]').forEach(node=>{
    const key=node.dataset.filterKey, value=node.type==='checkbox'?node.checked:node.value;
    if(['lowpass_enabled','lowpass_auto'].includes(key))out[key]=filterBoolean(value);
    else if(key==='mode')out[key]=String(value);
    else {
      const numeric=Number(value), fallback=Number(FILTER_DEFAULTS[key]);
      out[key]=value===''||!Number.isFinite(numeric)?(
        Number.isFinite(Number(state.filter[key]))&&Number(state.filter[key])>0
          ? Number(state.filter[key]) : fallback
      ):numeric;
    }
  });
  return out;
}
function filterPayload(){return {...state.filter}}
function filterEffectiveNote(meta){
  if(state.filter.mode==='off')return '滤波已关闭；图表、统计和导出均使用原始数据。';
  if(!state.filter.lowpass_enabled)return '低通已关闭；当前显示原始数据。';
  const fs=Number(meta?.sample_rate_hz), parts=[];
  if(Number.isFinite(fs)&&fs>0)parts.push(`实际采样 ${fmt(fs,3)} Hz（奈奎斯特 ${fmt(fs/2,3)} Hz）`);
  if(meta?.lowpass_cutoff_hz)parts.push(`低通 ${fmt(meta.lowpass_cutoff_hz,3)} Hz`);
  if(meta?.note)parts.push(meta.note);
  return parts.join(' · ');
}
function renderFilterControls(meta=null){
  filterPanels().forEach(panel=>{
    panel.classList.toggle('dirty',state.filterDirty);
    panel.querySelectorAll('[data-show-raw]').forEach(node=>{node.checked=state.showRaw});
    panel.querySelectorAll('[data-filter-key]').forEach(node=>{
      const key=node.dataset.filterKey, value=state.filter[key];
      if(node.type==='checkbox')node.checked=Boolean(value);
      else if(!(key==='lowpass_cutoff_hz'&&node===document.activeElement))node.value=String(value);
      const manual=key==='lowpass_cutoff_hz'&&!state.filter.lowpass_auto;
      if(key==='lowpass_cutoff_hz'){
        const enabled = key==='lowpass_cutoff_hz' && state.filter.lowpass_enabled && manual;
        node.disabled=!enabled;
      }
    });
    const status=panel.querySelector('[data-filter-status]');
    if(status){const text=state.filterDirty?'有未保存改动':filterEffectiveNote(meta);status.textContent=text;status.hidden=!text}
  });
}
function filterRate(times){
  const ds=[]; for(let i=1;i<(times||[]).length;i++){const d=Number(times[i])-Number(times[i-1]);if(Number.isFinite(d)&&d>0)ds.push(d)}
  ds.sort((a,b)=>a-b); const mid=Math.floor(ds.length/2);
  const dt=ds.length?(ds.length%2?ds[mid]:(ds[mid-1]+ds[mid])/2):0;
  return dt>0?1/dt:0;
}
function filterLowpass(values,fs,cutoff,order){
  const alpha=(2*Math.PI*cutoff/fs)/(1+2*Math.PI*cutoff/fs);
  const pass=arr=>{const out=[];let s=Number(arr[0]);for(const x of arr){s+=alpha*(Number(x)-s);out.push(s)}return out};
  let pad=Math.min(Math.max(3,Math.round(fs/Math.max(cutoff,.05))),values.length-1), out=pad?[...values.slice(0,pad).reverse(),...values,...values.slice(-pad).reverse()]:[...values];
  for(let i=0;i<Number(order);i++){out=pass(out);out=pass([...out].reverse()).reverse()} return pad?out.slice(pad,-pad):out;
}
function filterValues(times,values,valid){
  const raw=(values||[]).map(v=>Number(v)), cfg=state.filter||FILTER_DEFAULTS;
  const meta={sample_rate_hz:filterRate(times),applied:false,lowpass_cutoff_hz:null,note:''};
  if(cfg.mode==='off'||!raw.length||meta.sample_rate_hz<=0)return {values:raw,meta};
  const fs=meta.sample_rate_hz, nyquist=fs/2, notes=[]; let cutoff=null;
  if(cfg.lowpass_enabled){
    const requested=cfg.lowpass_auto?Math.max(.05,Math.min(2,fs*.2)):Number(cfg.lowpass_cutoff_hz);
    cutoff=Math.min(requested,nyquist*.9);
    if(!cfg.lowpass_auto&&requested>=nyquist*.9)notes.push(`低通截止频率已限制为奈奎斯特频率的 90%（${fmt(cutoff,4)} Hz）`);
  }
  meta.applied=Boolean(cutoff);meta.lowpass_cutoff_hz=cutoff;meta.note=notes.join(' · ');
  if(!meta.applied)return {values:raw,meta};
  const out=[...raw], ok=raw.map((value,index)=>valid?.[index]!==false&&Number.isFinite(value)&&Number.isFinite(Number(times?.[index]))); let i=0;
  while(i<out.length){while(i<out.length&&!ok[i])i++;const start=i;while(i<out.length&&ok[i])i++;const stop=i;if(stop-start<5)continue;let seg=out.slice(start,stop);if(cutoff)seg=filterLowpass(seg,fs,cutoff,cfg.lowpass_order);seg.forEach((v,j)=>{out[start+j]=v})}
  return {values:out,meta};
}
async function loadFilter(){try{const data=await api('/api/filter');state.filter={...FILTER_DEFAULTS,...(data.settings||{})};state.filterDirty=false;renderFilterControls();}catch{renderFilterControls()}}
async function saveFilter(){try{const data=await post('/api/filter/apply',filterPayload());state.filter={...FILTER_DEFAULTS,...(data.settings||{})};state.filterDirty=false;state.filterAutoCache=null;renderFilterControls();drawAll();drawDebug();toast('软件滤波设置已保存')}catch(e){toast(e.message)}}
const updateFilterFromPanel = (panel, event) => {
  if(!event.target.matches('[data-filter-key]'))return;
  state.filter=readFilterPanel(panel);state.filterDirty=true;state.filterAutoCache=null;renderFilterControls();drawAll();drawDebug();
};
filterPanels().forEach(panel=>panel.addEventListener('input',event=>updateFilterFromPanel(panel,event)));
filterPanels().forEach(panel=>panel.addEventListener('change',event=>updateFilterFromPanel(panel,event)));
document.querySelectorAll('[data-show-raw]').forEach(node=>node.addEventListener('change',()=>{
  state.showRaw=node.checked;persistShowRaw(state.showRaw);renderFilterControls();drawAll();drawDebug();
}));
document.querySelectorAll('[data-filter-apply]').forEach(button=>button.addEventListener('click',()=>void saveFilter()));

// 自动停止参数在方法条件和 Debug 中是同一份设置。两个面板只用
// data 属性绑定，避免复制 id 后某一页被静默忽略。
const PLATEAU_DEFAULTS = {
  segment_duration_s: 5, segment_count: 6, absolute_tolerance_nA: .1,
  relative_tolerance: .01, scatter_multiplier: 3, minimum_coverage_ratio: .6,
  maximum_gap_periods: 2.5, required_consecutive_windows: 2,
  spike_scale_multiplier: 7, spike_neighbor_multiplier: 3,
};
state.plateau = {settings: {...PLATEAU_DEFAULTS}, window_duration_s: 30};
state.plateauDirty = false;
state.plateauBusy = false;
state.plateauLockedByFormalRun = false;
function plateauPanels(){return [...document.querySelectorAll('[data-plateau-panel]')]}
function updatePlateauControlAvailability(){
  plateauPanels().forEach(panel=>{
    panel.querySelectorAll('input,button').forEach(control=>{
      control.disabled=state.plateauBusy||state.plateauLockedByFormalRun;
    });
  });
}
function setPlateauFormalRunLock(locked){
  state.plateauLockedByFormalRun=Boolean(locked);
  updatePlateauControlAvailability();
}
function setPlateauBusy(busy){
  state.plateauBusy=Boolean(busy);
  plateauPanels().forEach(panel=>{
    panel.setAttribute('aria-busy',String(state.plateauBusy));
  });
  updatePlateauControlAvailability();
}
function plateauWindowDuration(config=state.plateau?.settings){
  const explicit=Number(config?.window_duration_s);
  if(Number.isFinite(explicit)&&explicit>0)return explicit;
  const duration=Number(config?.segment_duration_s)*Number(config?.segment_count);
  if(Number.isFinite(duration)&&duration>0)return duration;
  const stored=Number(state.plateau?.window_duration_s);
  return Number.isFinite(stored)&&stored>0?stored:30;
}
function renderPlateauControls(data=state.plateau,statusText=''){
  const settings={...PLATEAU_DEFAULTS,...(data?.settings||{})};
  const suppliedWindow=Number(data?.window_duration_s), windowDuration=
    Number.isFinite(suppliedWindow)&&suppliedWindow>0?suppliedWindow:
      Number(settings.segment_duration_s)*Number(settings.segment_count);
  state.plateau={settings,window_duration_s:windowDuration};
  plateauPanels().forEach(panel=>{
    panel.classList.toggle('dirty',state.plateauDirty);
    panel.querySelectorAll('[data-plateau-key]').forEach(input=>{
      if(input!==document.activeElement)input.value=String(settings[input.dataset.plateauKey]);
    });
    const status=panel.querySelector('[data-plateau-status]');
    if(status)status.textContent=statusText||(state.plateauDirty?'有未保存改动':'已与后台同步');
    const windowLabel=panel.querySelector('[data-plateau-window]');
    if(windowLabel)windowLabel.textContent=`${fmt(windowDuration,windowDuration%1?1:0)} s`;
  });
}
function readPlateauPanel(panel){
  const settings={};
  panel.querySelectorAll('[data-plateau-key]').forEach(input=>{settings[input.dataset.plateauKey]=Number(input.value)});
  return settings;
}
async function loadPlateau(){
  setPlateauBusy(true);
  try{state.plateauDirty=false;renderPlateauControls(await api('/api/plateau'))}
  catch{renderPlateauControls(state.plateau,'暂时无法读取后台设置')}
  finally{setPlateauBusy(false)}
}
async function savePlateau(panel,button){
  if(state.plateauBusy||state.plateauLockedByFormalRun)return;
  try{
    setPlateauBusy(true);button.textContent='正在保存…';
    const data=await post('/api/plateau/apply',readPlateauPanel(panel));
    state.plateauDirty=false;renderPlateauControls(data);if(state.settings)renderSettings(state.settings);drawDebug();toast('自动停止判定参数已保存');
  }catch(e){toast(e.message)}
  finally{setPlateauBusy(false);button.textContent='保存判定参数'}
}
plateauPanels().forEach(panel=>{
  panel.addEventListener('input',event=>{
    const input=event.target.closest('[data-plateau-key]');if(!input)return;
    state.plateauDirty=true;
    document.querySelectorAll(`[data-plateau-key="${input.dataset.plateauKey}"]`).forEach(peer=>{if(peer!==input)peer.value=input.value});
    const draft={settings:readPlateauPanel(panel)};
    renderPlateauControls(draft,'有未保存改动');
  });
  panel.querySelector('[data-plateau-apply]').addEventListener('click',event=>void savePlateau(panel,event.currentTarget));
});
renderPlateauControls(state.plateau,'正在读取后台设置');
function concentrationValue(){return $('knownConcentration').value===''?null:$('knownConcentration').value}
function scaledConcentrationValue(raw,factor){
  const text=String(raw??'').trim(),value=Number(text),multiplier=Number(factor);
  if(text===''||!Number.isFinite(value)||value<0||!Number.isFinite(multiplier)||multiplier<=0)return null;
  const scaled=value*multiplier;
  if(!Number.isFinite(scaled)||scaled<0)return null;
  return String(Number(scaled.toPrecision(12)));
}
function scaleKnownConcentration(factor){
  const input=$('knownConcentration'),scaled=scaledConcentrationValue(input.value,factor);
  if(scaled===null){toast('请先输入有效的非负浓度');return false}
  input.value=scaled;
  input.dispatchEvent(new Event('input',{bubbles:true}));
  return true;
}
function previewFilename(){const sample=($('sampleName').value||'样品名称').trim().replace(/[\\/:*?"<>|]/g,'_'), concentration=concentrationValue();$('autoSaveName').textContent=`${sample}-${concentration===null?'unknown':`${Number(concentration)}uM`}.csv`}
function updateStartState(){
  const running=state.measurement?.busy??state.measurement?.state==='running', ready=state.workflow?.calibration_ready;
  const cv=state.method==='cv';
  $('startMeasure').disabled=running||!state.settings?.applied||!state.workflow?.save_dir||(!cv&&state.sampleRole==='test'&&!ready);
  $('startMeasure').textContent=cv?'开始 CV 扫描':state.sampleRole==='calibration'?'开始标定测量':'开始测试并预测';
}
function setSampleRole(role, quiet=false){
  if(role==='test'&&!state.workflow?.calibration_ready){if(!quiet)toast('请先选择标定点并生成测试曲线');return}
  state.sampleRole=role;
  $('sampleRole').querySelectorAll('button').forEach(button=>button.classList.toggle('active',button.dataset.role===role));
  $('concentrationLabel').textContent=role==='calibration'?'已知浓度（标定必填）':'已知浓度（可选，用于验证）';
  $('knownConcentration').placeholder=role==='calibration'?'请输入标定浓度':'可留空，由模型预测';
  $('predictionResult').hidden=role!=='test';
  updateStartState();previewFilename();
}
function renderWorkflow(data){
  state.workflow=data;$('saveDirectory').value=data.save_dir||'';
  const cv=state.method==='cv';
  document.querySelector('.workflow-steps').hidden=cv;
  $('resetCalibration').hidden=cv;
  const labels={collect:'标定采集',select:'待选范围',stabilization:'稳定化中',test:'测试就绪'};
  $('workflowBadge').textContent=cv?'自动保存':labels[data.stage]||'待配置';$('workflowBadge').className=`live-badge ${cv||data.calibration_ready?'running':''}`;
  $('workflowMessage').textContent=cv?'CV 原始点、标准 CSV、汇总与曲线图将在扫描结束后写入当前目录':data.calibration_ready?`测试曲线采用 ${data.selected_points_count} / ${data.points_count} 个候选点；后续采集不会自动改写`:data.points_count&&!data.settings_match?'当前 IT 条件与已有标定不同；请恢复原条件或新建标定批次':`已记录 ${data.points_count} 个候选点，请到“标定与漂移”选择用于拟合的范围`;
  $('calibrationStep').classList.toggle('active',data.stage==='collect');$('selectionStep').classList.toggle('active',data.stage==='select');$('testStep').classList.toggle('active',['stabilization','test'].includes(data.stage));
  const testButton=$('sampleRole').querySelector('[data-role="test"]');testButton.disabled=!data.calibration_ready;
  ['test','stabilization'].forEach(role=>$('scheduleRole').querySelector(`option[value="${role}"]`).disabled=!data.calibration_ready);
  if(!data.calibration_ready&&['test','stabilization'].includes($('scheduleRole').value))$('scheduleRole').value='calibration';
  if(!cv&&!data.calibration_ready&&state.sampleRole==='test')setSampleRole('calibration',true);
  const latest=data.latest_result;if(latest){$('savedResult').hidden=false;$('savedResultPath').textContent=latest.data_path||latest.raw_path||latest.summary_path||''}
  renderScheduleMode();
  updateStartState();
}
async function handleWorkflowCompletion(data){
  const result=data.workflow_result;if(!result||state.lastHandledRunId===data.run_id)return;
  state.lastHandledRunId=data.run_id;
  try{renderWorkflow(await api('/api/workflow'));state.calibration=await api('/api/calibration');renderCalibration();if(result.sample_role==='stabilization')renderDrift(await api('/api/drift'))}catch{}
  $('savedResult').hidden=false;$('savedResultPath').textContent=result.data_path||result.raw_path||result.summary_path||'';
  if(result.export_error){toast(`自动保存失败：${result.export_error}`);return}
  if(result.state!=='completed'){toast('未完成的数据已保存为原始文件');return}
  if(result.sample_role==='cv'){
    toast('CV 扫描完成，全部原生电流点已保存');
  }else if(result.sample_role==='calibration'){
    toast('候选标定点已保存；当前测试曲线未被改写');
  }else if(result.sample_role==='stabilization'){
    toast('稳定化 IT 已保存；测试曲线保持锁定');
  }else{
    $('predictionResult').hidden=false;$('predictionResult').querySelector('strong').textContent=fmt(result.predicted_concentration_um,3);
    toast('测试完成，浓度已自动预测并保存');
  }
}

function workspaceHasUnsavedChanges(){
  const directoryDraft=Boolean(state.workflow&&$('saveDirectory').value.trim()!==String(state.workflow.save_dir||''));
  return Boolean(state.calibrationDirty||state.validationDirty||state.settingsDirty||state.filterDirty||state.plateauDirty||state.driftDirty||directoryDraft);
}
function historyStatusLabel(status){return {available:'可用',missing:'目录缺失',corrupt:'数据损坏'}[status]||'不可用'}
function historyTimestamp(value){const numeric=Number(value);return Number.isFinite(numeric)&&numeric>0?new Date(numeric*1000).toLocaleString('zh-CN',{hour12:false}):'时间未知'}
function historyStat(label,value){const node=document.createElement('div'),name=document.createElement('span'),number=document.createElement('strong');name.textContent=label;number.textContent=String(value??0);node.append(name,number);return node}
function renderWorkspaceHistory(data=state.history){
  state.history=data||{entries:[]};
  const all=state.history.entries||[],favoritesOnly=$('historyFavoritesOnly').checked;
  const workspaces=state.history.workspaces||all.filter(entry=>entry.kind!=='batch');
  const select=$('historyWorkspaceSelect'),previous=select.value;
  select.replaceChildren();
  workspaces.forEach(entry=>{
    const option=document.createElement('option');option.value=entry.workspace_id;option.textContent=`${entry.label} · ${entry.location}`;select.appendChild(option);
  });
  const preferred=workspaces.some(entry=>entry.workspace_id===previous)?previous:
    (state.history.active_workspace_id&&workspaces.some(entry=>entry.workspace_id===state.history.active_workspace_id)?state.history.active_workspace_id:(workspaces[0]?.workspace_id||''));
  select.value=preferred;
  const selectedWorkspace=workspaces.find(entry=>entry.workspace_id===preferred);
  const scoped=preferred?all.filter(entry=>entry.workspace_id===preferred||(entry.kind==='batch'&&entry.workspace_root_id===preferred)):all;
  const entries=favoritesOnly?scoped.filter(entry=>entry.favorite):scoped;
  const batchCount=scoped.filter(entry=>entry.kind==='batch').length;
  $('workspaceHistorySummary').textContent=`${workspaces.length} 个工作区 · 当前 ${batchCount} 个批次 · ${all.filter(entry=>entry.favorite).length} 个收藏`;
  $('historyBatchSummary').textContent=selectedWorkspace?`当前工作区：${selectedWorkspace.label} · ${batchCount} 个批次`:'暂无可用工作区';
  const error=$('workspaceHistoryError'),registryError=String(state.history.registry_error||'');
  error.textContent=registryError;error.hidden=!registryError;
  const list=$('workspaceHistoryList');list.replaceChildren();
  if(!entries.length){const empty=document.createElement('div');empty.className='workspace-history-empty';empty.textContent=favoritesOnly?'暂无收藏的历史记录':'暂无历史记录';list.appendChild(empty);return}
  entries.forEach(entry=>{
    const card=document.createElement('article');card.className=`workspace-history-card ${entry.current?'current ':''}${entry.status==='available'?'':'unavailable'}`.trim();card.dataset.workspaceId=entry.workspace_id;
    const title=document.createElement('div');title.className='workspace-history-title';
    const identity=document.createElement('div'),name=document.createElement('strong'),location=document.createElement('small');name.textContent=`${entry.current?'当前 · ':''}${entry.kind==='batch'?'批次 · ':'工作区 · '}${entry.label}`;location.textContent=entry.location;identity.append(name,location);
    const star=document.createElement('button');star.type='button';star.className=`workspace-history-star ${entry.favorite?'active':''}`;star.textContent=entry.favorite?'★':'☆';star.title=entry.favorite?'取消收藏':'收藏';star.setAttribute('aria-label',star.title);star.onclick=()=>void toggleWorkspaceFavorite(entry);
    title.append(identity,star);
    const meta=document.createElement('div');meta.className='workspace-history-meta';const timestamp=document.createElement('span'),status=document.createElement('strong');timestamp.textContent=historyTimestamp(entry.summary?.latest_result_at||entry.updated_at);status.className=`workspace-history-status ${entry.status}`;status.textContent=historyStatusLabel(entry.status);status.title=entry.status_detail||'';meta.append(timestamp,status);
    const stats=document.createElement('div');stats.className='workspace-history-stats';stats.append(historyStat('标定点',entry.summary?.points_count),historyStat('测试',entry.summary?.test_count),historyStat('模型',entry.summary?.has_model?`R² ${fmt(entry.summary?.model_r2,3)}`:'--'));
    const actions=document.createElement('div');actions.className='workspace-history-actions';const open=document.createElement('button');open.type='button';open.className='secondary';open.textContent=entry.current?'当前已打开':'打开并恢复';open.disabled=entry.current||entry.status!=='available';open.onclick=()=>void openWorkspaceHistory(entry);const remove=document.createElement('button');remove.type='button';remove.className='text-button';remove.textContent='移除入口';remove.onclick=()=>void removeWorkspaceHistory(entry);actions.append(open,remove);
    card.append(title,meta,stats,actions);list.appendChild(card);
  });
}
async function refreshWorkspaceHistory(){try{renderWorkspaceHistory(await api('/api/history'))}catch(e){errorBox('workspaceHistoryError',e)}}
async function toggleWorkspaceFavorite(entry){try{await post('/api/history/favorite',{workspace_id:entry.workspace_id,favorite:!entry.favorite});await refreshWorkspaceHistory();toast(entry.favorite?'已取消收藏':'已收藏')}catch(e){errorBox('workspaceHistoryError',e)}}
async function removeWorkspaceHistory(entry){
  if(!confirm(`从历史列表移除“${entry.label}”？\n\n原始测量目录和数据不会被删除。`))return;
  try{renderWorkspaceHistory(await post('/api/history/remove',{workspace_id:entry.workspace_id}));toast('历史入口已移除，原始数据保留')}catch(e){errorBox('workspaceHistoryError',e)}
}
async function openWorkspaceHistory(entry){
  const unsaved=workspaceHasUnsavedChanges();
  if(unsaved&&!confirm('当前页面有未保存编辑。放弃这些修改并恢复历史工作区？'))return;
  try{
    const result=await post('/api/history/open',{workspace_id:entry.workspace_id,unsaved_changes:unsaved,discard_unsaved:unsaved});
    state.settingsDirty=state.calibrationDirty=state.validationDirty=state.filterDirty=state.plateauDirty=state.driftDirty=false;
    state.workflow=result.workflow;state.calibration=result.calibration;state.settings=result.settings;state.filter={...FILTER_DEFAULTS,...(result.filter?.settings||{})};state.plateau=result.plateau;renderSettings(state.settings);renderWorkflow(state.workflow);renderFilterControls();renderPlateauControls(state.plateau);renderCalibration();try{renderDrift(await api('/api/drift'))}catch{}renderWorkspaceHistory(result.history);setSampleRole(state.workflow?.calibration_ready?'test':'calibration',true);
    document.querySelector('.nav-item[data-view="measure"]').click();toast('已恢复历史工作区，继续测量前请重新应用硬件条件');
  }catch(e){errorBox('workspaceHistoryError',e)}
}
$('historyFavoritesOnly').addEventListener('change',()=>renderWorkspaceHistory());
$('historyWorkspaceSelect').addEventListener('change',()=>renderWorkspaceHistory());
$('refreshWorkspaceHistory').addEventListener('click',()=>void refreshWorkspaceHistory());
$('registerCurrentHistory').addEventListener('click',async()=>{try{await post('/api/history/register',{});await refreshWorkspaceHistory();toast('当前工作区已登记')}catch(e){errorBox('workspaceHistoryError',e)}});
$('importHistoryDirectory').addEventListener('click',async()=>{
  const path=$('historyImportPath').value.trim();
  if(!path){errorBox('workspaceHistoryError',new Error('请填写历史数据目录'));return}
  try{
    renderWorkspaceHistory(await post('/api/history/import',{path}));
    $('historyImportPath').value='';
    toast('历史数据目录已导入');
  }catch(e){errorBox('workspaceHistoryError',e)}
});

document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x === button));
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  $(`view-${button.dataset.view}`).classList.add('active');
  $('pageTitle').textContent = pages[button.dataset.view][0];
  $('pageSubtitle').textContent = pages[button.dataset.view][1];
  if(button.dataset.view==='history')void refreshWorkspaceHistory();
  requestAnimationFrame(() => {drawAll(); drawDebug();});
}));

function setMeasureControlTab(name) {
  $('measureControlTabs').querySelectorAll('[data-control-tab]').forEach(button => {
    const active = button.dataset.controlTab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('[data-control-panel]').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.controlPanel === name);
  });
}
$('measureControlTabs').addEventListener('click', event => {
  const button = event.target.closest('[data-control-tab]');
  if (button) setMeasureControlTab(button.dataset.controlTab);
});

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1, rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, rect.width * ratio); canvas.height = Math.max(1, rect.height * ratio);
  const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); return {ctx, w: rect.width, h: rect.height};
}
/* 双轴是 **opt-in 扩展**:series 里带 `axis:'right'` 的走右轴,
 * options 可给 y2label / y2Digits / yDigits。不传 `axis` 时行为与改动前**像素级相同**
 * ⇒ 现有两个调用点(itChart / calibrationChart)一个字都不用改。
 * 左右轴共用同一组分数网格位置(6 条线),否则两侧刻度视觉上对不齐,
 * 人会误读成"两条线在同一个值上交叉"。 */
function drawChart(canvas, series, options = {}) {
  const {ctx, w, h} = setupCanvas(canvas); ctx.clearRect(0, 0, w, h);
  const left = series.filter(s => s.axis !== 'right'), right = series.filter(s => s.axis === 'right');
  const hasRight = right.length > 0;
  const pts = list => list.flatMap(s => s.points).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  const allL = pts(left), allR = pts(right), all = allL.concat(allR);
  if (!all.length) return;
  let xmin = options.xmin ?? Math.min(...all.map(p => p[0])), xmax = options.xmax ?? Math.max(...all.map(p => p[0]));
  const span = list => {
    if (!list.length) return [0, 1];
    let lo = Math.min(...list.map(p => p[1])), hi = Math.max(...list.map(p => p[1]));
    if (hi === lo) {lo -= 1; hi += 1;}
    const pad = (hi - lo) * .12; return [lo - pad, hi + pad];
  };
  let [ymin, ymax] = span(allL);
  const [y2min, y2max] = span(allR);
  if (xmax === xmin) xmax = xmin + 1;
  const m = {l:56, r: hasRight ? 56 : 18, t:18, b:38};
  const px = x => m.l+(x-xmin)/(xmax-xmin)*(w-m.l-m.r);
  const py = y => m.t+(ymax-y)/(ymax-ymin)*(h-m.t-m.b);
  const py2 = y => m.t+(y2max-y)/(y2max-y2min)*(h-m.t-m.b);
  const yd = options.yDigits ?? 1, y2d = options.y2Digits ?? 0;
  if(options.bands?.length){
    ctx.save();ctx.beginPath();ctx.rect(m.l,m.t,(w-m.l-m.r),(h-m.t-m.b));ctx.clip();
    options.bands.forEach(band=>{
      const x0=Math.max(xmin,Number(band.x0)),x1=Math.min(xmax,Number(band.x1));
      if(!Number.isFinite(x0)||!Number.isFinite(x1)||x1<=x0)return;
      ctx.fillStyle=band.color||'rgba(17,122,101,.06)';ctx.fillRect(px(x0),m.t,px(x1)-px(x0),h-m.t-m.b);
    });
    ctx.restore();
  }
  ctx.font = '10px system-ui'; ctx.fillStyle='#718086'; ctx.strokeStyle='#e2e7e8'; ctx.lineWidth=1;
  for(let i=0;i<=5;i++){
    const f=i/5, yy=m.t+(1-f)*(h-m.t-m.b);
    ctx.beginPath();ctx.moveTo(m.l,yy);ctx.lineTo(w-m.r,yy);ctx.stroke();
    ctx.fillStyle='#718086';ctx.fillText((ymin+(ymax-ymin)*f).toFixed(yd),6,yy+3);
    if(hasRight){ctx.fillStyle='#8a6fb0';ctx.fillText((y2min+(y2max-y2min)*f).toFixed(y2d),w-m.r+6,yy+3)}
  }
  ctx.fillStyle='#718086';
  for(let i=0;i<=6;i++){const x=xmin+(xmax-xmin)*i/6, xx=px(x);ctx.beginPath();ctx.moveTo(xx,m.t);ctx.lineTo(xx,h-m.b);ctx.stroke();ctx.fillText(x.toFixed(xmax<=60?1:0),xx-8,h-15)}
  series.forEach(s => {
    const points = s.points.filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
    const yf = s.axis === 'right' ? py2 : py;
    const width = s.width ?? 1.6;
    if (width > 0 && points.length) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = width;
      if (s.dash) ctx.setLineDash(s.dash);
      ctx.beginPath();
      points.forEach((p, i) => i ? ctx.lineTo(px(p[0]), yf(p[1])) : ctx.moveTo(px(p[0]), yf(p[1])));
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (s.dots) {
      ctx.fillStyle = s.color;
      const radius = s.pointRadius ?? 4;
      points.forEach(p => {
        ctx.beginPath();
        ctx.arc(px(p[0]), yf(p[1]), radius, 0, Math.PI * 2);
        ctx.fill();
      });
    }
  });
  ctx.fillStyle='#68767b';ctx.fillText(options.xlabel||'Time (s)',w/2-22,h-2);
  ctx.save();ctx.translate(12,h/2+25);ctx.rotate(-Math.PI/2);ctx.fillText(options.ylabel||'Current (nA)',0,0);ctx.restore();
  if(hasRight){ctx.fillStyle='#8a6fb0';ctx.save();ctx.translate(w-10,h/2-25);ctx.rotate(Math.PI/2);ctx.fillText(options.y2label||'',0,0);ctx.restore()}
}
function apBounds(x,zone){
  if(x<10)return zone==='blue'?[x-2,x+2]:[Math.max(0,x-4),x+4];
  return zone==='blue'?[.8*x,1.2*x]:[.6*x,1.4*x];
}
function hasFiniteConcentration(point){
  return point.concentration_um!==null&&point.concentration_um!==''&&Number.isFinite(Number(point.concentration_um));
}
function apChartDomain(){
  return {xMax:50,yMax:50,minValue:0};
}
function drawApScoreChart(points){
  const canvas=$('apScoreChart'),{ctx,w,h}=setupCanvas(canvas);ctx.clearRect(0,0,w,h);
  const candidates=(points||[]).filter(hasFiniteConcentration),valid=candidates.filter(p=>p.predicted_concentration_um!==null&&p.predicted_concentration_um!==''&&Number.isFinite(Number(p.predicted_concentration_um))),missing=candidates.filter(p=>p.predicted_concentration_um===null||p.predicted_concentration_um===''||!Number.isFinite(Number(p.predicted_concentration_um)));
  const {xMax,yMax,minValue}=apChartDomain(),ySpan=yMax-minValue;
  const m={l:52,r:18,t:18,b:52},px=x=>m.l+x/xMax*(w-m.l-m.r),py=y=>h-m.b-(y-minValue)/ySpan*(h-m.t-m.b);
  const band=(zone,color)=>{ctx.fillStyle=color;ctx.beginPath();for(let i=0;i<=120;i++){const x=xMax*i/120,y=apBounds(x,zone)[1];i?ctx.lineTo(px(x),py(y)):ctx.moveTo(px(x),py(y))}for(let i=120;i>=0;i--){const x=xMax*i/120;ctx.lineTo(px(x),py(apBounds(x,zone)[0]))}ctx.closePath();ctx.fill()};
  ctx.save();ctx.beginPath();ctx.rect(m.l,m.t,w-m.l-m.r,h-m.t-m.b);ctx.clip();ctx.fillStyle='#f1eeee';ctx.fillRect(m.l,m.t,w-m.l-m.r,h-m.t-m.b);band('green','#cfe8dc');band('blue','#cfe1e8');ctx.restore();
  ctx.font='10px system-ui';ctx.strokeStyle='#e2e7e8';ctx.lineWidth=1;ctx.fillStyle='#718086';
  for(let i=0;i<=5;i++){const x=xMax*i/5,xx=px(x),y=minValue+ySpan*i/5,yy=py(y);ctx.beginPath();ctx.moveTo(xx,m.t);ctx.lineTo(xx,h-m.b);ctx.stroke();ctx.fillText(x.toFixed(0),xx-6,h-28);ctx.beginPath();ctx.moveTo(m.l,yy);ctx.lineTo(w-m.r,yy);ctx.stroke();ctx.fillText(y.toFixed(0),20,yy+3)}
  ctx.save();ctx.beginPath();ctx.rect(m.l,m.t,w-m.l-m.r,h-m.t-m.b);ctx.clip();
  ctx.setLineDash([5,4]);ctx.strokeStyle='#6d7b7d';ctx.beginPath();ctx.moveTo(px(0),py(0));ctx.lineTo(px(xMax),py(xMax));ctx.stroke();ctx.setLineDash([]);
  [['blue','#28708c'],['green','#c48720'],['grey','#b54455']].forEach(([zone,color])=>{ctx.fillStyle=color;valid.filter(p=>p.zone===zone).forEach(p=>{ctx.beginPath();ctx.arc(px(Number(p.concentration_um)),py(Number(p.predicted_concentration_um)),4,0,Math.PI*2);ctx.fill()})});
  if(missing.length){ctx.strokeStyle='#7b686c';ctx.fillStyle='#7b686c';ctx.lineWidth=1.5;missing.forEach(p=>{const x=px(Number(p.concentration_um)),y=py(minValue+ySpan*.94);ctx.beginPath();ctx.moveTo(x-5,y-5);ctx.lineTo(x+5,y+5);ctx.moveTo(x+5,y-5);ctx.lineTo(x-5,y+5);ctx.stroke();ctx.font='bold 10px system-ui';ctx.fillText('?',x+7,y+3)})}
  ctx.restore();
  ctx.fillStyle='#68767b';ctx.fillText('真实浓度 x (µM)',w/2-42,h-8);ctx.save();ctx.translate(12,h/2+28);ctx.rotate(-Math.PI/2);ctx.fillText('测量浓度 y (µM)',0,0);ctx.restore();
}
function drawAll(){
  const d = state.measurement?.data || {};
  const current = d.current_nA || [], allSeries=[];
  const filtered = filterValues(d.time_s || current.map((_,i)=>i), current, d.valid).values;
  if(state.method==='cv' && d.potential_v){
    const maxCycle=Math.max(0,...(d.cycle||[]));
    const keep=state.chartWindowS===null?Infinity:state.chartWindowS;
    const firstCycle=Math.max(1,maxCycle-keep+1);
    for(let cycle=firstCycle;cycle<=maxCycle;cycle++){
      const rawValid=[],filteredValid=[],invalid=[];
      d.potential_v.forEach((potential,i)=>{
        if(d.cycle[i]!==cycle)return;
        const rawPoint=[potential,Number(current[i])/1000], filteredPoint=[potential,Number(filtered[i])/1000];
        if(d.valid?.[i]===false)invalid.push(rawPoint);
        else {rawValid.push(rawPoint);filteredValid.push(filteredPoint)}
      });
      if(state.showRaw&&rawValid.length)allSeries.push({points:rawValid,color:cycle===maxCycle?'#aab5b7':'#c5cdcf',width:cycle===maxCycle ? .65 : .35});
      if(filteredValid.length)allSeries.push({points:filteredValid,color:'#117a65',width:cycle===maxCycle?1.2:.55,dots:true,pointRadius:cycle===maxCycle?1.25:.8});
      if(state.showRaw&&invalid.length)allSeries.push({points:invalid,color:'#b94444',width:0,dots:true,pointRadius:1.6});
    }
    $('chartEmpty').hidden=allSeries.some(series=>series.points.length>0);
    drawChart($('itChart'),allSeries,{xmin:Number(state.settings?.settings?.cv_low_v??-.6),xmax:Number(state.settings?.settings?.cv_high_v??.6),xlabel:'电位 vs RE (V)',ylabel:'电流 (µA)'});
  }else{
    const rawPoints=[],validPoints=[],invalidPoints=[],filteredPoints=[];
    (d.time_s||[]).forEach((time,i)=>{const point=[time,current[i]];rawPoints.push(point);(d.valid?.[i]===false?invalidPoints:validPoints).push(point);if(d.valid?.[i]!==false)filteredPoints.push([time,filtered[i]])});
    const duration=Number(state.measurement?.settings?.duration_s||state.settings?.settings?.duration_s||180),adaptive=Boolean(state.measurement?.settings?.adaptive_stop??state.settings?.settings?.adaptive_stop),latest=rawPoints.at(-1)?.[0]||0;
    if(state.chartWindowFixed){while(latest>state.chartWindowS)state.chartWindowS+=100}
    const xmin=state.chartWindowFixed?0:Math.max(0,latest-state.chartWindowS),xmax=state.chartWindowFixed?Math.max(300,state.chartWindowS):Math.max(state.chartWindowS,latest),visible=points=>points.filter(point=>point[0]>=xmin&&point[0]<=xmax);
    if(state.showRaw)allSeries.push({points:visible(rawPoints),color:'#b8c0c2',width:.55},
      {points:visible(validPoints),color:'#9aa7aa',width:0,dots:true,pointRadius:1.1});
    allSeries.push({points:visible(filteredPoints),color:'#167b74',width:1.45});
    if(state.showRaw)allSeries.push({points:visible(invalidPoints),color:'#c33c54',width:0,dots:true,pointRadius:1.7});
    $('chartEmpty').hidden=allSeries.some(series=>series.points.length>0);
    const meta=filterValues(d.time_s||[],current,d.valid).meta;renderFilterControls(meta);drawChart($('itChart'),allSeries,{xmin,xmax,xlabel:'时间 (s)',ylabel:'电流 (nA)'});
  }
  const c=state.calibration, series=[];
  if(c.curve)series.push({points:c.curve.concentration_um.map((x,i)=>[x,c.curve.current_nA[i]]),color:'#c77a18',width:2});
  const selected=(c.points||[]).filter(p=>p.selected);
  if(state.showCalibrationPoints&&selected.length)series.push({points:selected.map(p=>[Number(p.concentration_um),Number(p.current_nA)]),color:'#28708c',dots:true,width:0,pointRadius:4});
  const validation=(c.validation_points||[]).filter(p=>hasFiniteConcentration(p)&&Number.isFinite(Number(p.current_nA)));
  if(state.showTestPoints&&validation.length)series.push({points:validation.map(p=>[Number(p.concentration_um),Number(p.current_nA)]),color:'#b54455',dots:true,width:0,pointRadius:4.5});
  $('calibrationEmpty').hidden=series.length>0;drawChart($('calibrationChart'),series,{xlabel:'浓度 (µM)',ylabel:'电流 (nA)'});
  drawApScoreChart(c.validation_points||[]);
}
[
  ['showCalibrationPoints','showCalibrationPoints'],
  ['showTestPoints','showTestPoints'],
].forEach(([id,key])=>{
  const input=$(id);input.checked=state[key];input.addEventListener('change',()=>{
    state[key]=input.checked;persistCalibrationSeriesVisibility(key,state[key]);drawAll();
  });
});

// ── 方案 C:运行档位(硬件真值)────────────────────────────────────────────
// 🔴 刻意与「IT 条件」里的 fsr_nA/offset_nA 分开显示:那两个是**最后一次烧录进去的
//    编译期默认值**,而 RANGE 命令能在运行中改掉实际档位,两者可以不一致。
//    唯一权威来源是固件回的 RANGE_APPLIED(服务端从 rtt.log 增量解析)。
const FSR_LABEL=['50 nA','100 nA','250 nA','500 nA','1 µA','2 µA'];
const OFF_LABEL=['0','10% FSR','20% FSR','50% FSR','9 nA','19 nA','40 nA','80 nA'];
function renderRange(data){
  const r=data.range_runtime||{}, box=$('rangeLive'), running=data.state==='running';
  const a=r.applied;
  box.classList.toggle('switching', Boolean(r.pending));
  if(r.pending){
    $('rangeBadge').textContent='动态量程切换中…'; $('rangeBadge').className='live-badge';
    $('rangeDetail').textContent=`已下发 ${r.pending},等固件回 RANGE_APPLIED`;
  } else if(r.rejected){
    $('rangeBadge').textContent='切档被拒'; $('rangeBadge').className='live-badge error';
    $('rangeDetail').textContent=r.rejected;
  } else if(a){
    $('rangeBadge').textContent='已切档'; $('rangeBadge').className='live-badge running';
    $('rangeDetail').textContent=`还原侧 ≤${fmt(a.red_max_pa/1000,1)} nA · 氧化侧 ≤${fmt(a.ox_max_pa/1000,1)} nA · sat 余量 ${a.sat_margin} counts`;
  } else {
    $('rangeBadge').textContent=running?'编译期默认':'未知';
    $('rangeBadge').className='live-badge';
    $('rangeDetail').textContent=running
      ? '本轮尚未在线切档,当前用的是烧录进去的默认档位'
      : '开始测量后可在线切档,不复位、不中断极化';
  }
  $('rangeNow').textContent = a
    ? `${FSR_LABEL[a.fsr_code]} / offset ${fmt(a.off_pa/1000,0)} nA · ${a.bits} bit · LSB ${fmt(a.lsb_eff_fa/1000,1)} pA`
    : '—';
  // 只有测量进行中才能切:命令要经采集器的 RTT socket 转发,采集器不在就没人转发
  $('applyRange').disabled=!running;
  renderReflashWarn(a);
}
// 🔴 运行时档位 ≠ 已烧录 settings 时告警。点「应用条件并烧录硬件」会重编译+烧录+
//    **复位 MCU**,复位中断极化 ⇒ 档位回退到编译期默认,并重新引入初始瞬态
//    (实测撞轨 7~91s,期间恒电位环开环)。这个后果在点之前必须可见。
function renderReflashWarn(applied){
  const node=$('reflashWarn'), s=state.settings?.settings;
  if(!applied||!s){ node.hidden=true; return }
  const liveFsr=Math.round(applied.fsr_pa/1000), liveOff=Math.round(applied.off_pa/1000);
  const same = liveFsr===Number(s.fsr_nA) && Math.abs(liveOff-Number(s.offset_nA))<=1;
  node.hidden=same;
  if(!same) node.textContent=
    `⚠️ 硬件当前跑的是 ${liveFsr} nA / offset ${liveOff} nA(在线切档结果),`+
    `而上面这组条件是 ${s.fsr_nA} nA / offset ${s.offset_nA} nA。`+
    `点「应用条件并烧录硬件」会复位 MCU ⇒ 丢掉在线切档结果、并重新引入初始瞬态。`;
}
// 两相测量。工作点 E=+200mV 驱动**氧化**,信号走器件原生方向、不受 offset 限制;
// 但复位放生电极后的**起始瞬态是还原方向**(实测起点 ≥500nA),而还原侧上限就是
// offset。所以:瞬态期要大 offset(否则撞轨、电极根本不在 +200mV),测量期要小
// offset(它只白占量程+白加容差)。两者靠 RANGE 在线切档分开在时间上满足。
// 电极电位连采。E = V_WE − V_RE 必须由两路相减 —— 断开放大器时 RE 同样在浮,
// 只看 WE 对芯片 GND 的电压没有电化学意义。code 撞 0/4095 = 超 System ADC 量程。
function renderCellV(data){
  const c=data.cell_v, box=$('cellvLive');
  if(!c){
    $('cellvBadge').textContent='无数据'; $('cellvBadge').className='live-badge';
    $('cellvE').textContent='—';
    $('cellvDetail').textContent='固件按 SYS_PERIOD(≈1Hz)与电流并行采样;idle 与测量期间都采';
    box.classList.remove('clipped'); return;
  }
  const clipped=Boolean(c.clipped);
  box.classList.toggle('clipped',clipped);
  $('cellvBadge').textContent=clipped?'超量程削顶':`${c.rows} 组`;
  $('cellvBadge').className=`live-badge ${clipped?'error':'running'}`;
  $('cellvE').textContent=`E = ${fmt(c.e_mv,0)} mV`;
  $('cellvDetail').textContent=
    `WE ${fmt(c.we_mv,0)} · RE ${fmt(c.re_mv,0)} · CE ${fmt(c.ce_mv,0)} · WO ${fmt(c.wo_mv,0)} mV`+
    `  |  code WE ${c.we_code} / RE ${c.re_code}`+
    (clipped?'  ⚠️ 有 code 撞 0 或 4095,电位已超出 0~3.07V 可测范围':'');
}
function renderTransient(data){
  const p=data.transient||{}, auto=data.auto_switch||{}, box=$('phaseLive');
  const running=data.state==='running', atTarget=isMeasRange(data.range_runtime?.applied);
  box.classList.toggle('reduction', p.phase==='reduction');
  box.classList.toggle('ready', Boolean(p.ready)||atTarget);
  const drift=p.drift_pa_s==null?null:Math.abs(p.drift_pa_s);
  const driftTxt=drift==null?'—':`${fmt(drift,1)} pA/s`;
  if(!p.phase||p.phase==='idle'){
    $('phaseBadge').textContent=running?'等首个样本':'待机';
    $('phaseBadge').className='live-badge';
    $('phaseNow').textContent='—';
    $('phaseDetail').textContent='开始测量后显示：还原瞬态 → 过零 → 氧化稳态';
  } else if(p.phase==='reduction'){
    $('phaseBadge').textContent='还原瞬态'; $('phaseBadge').className='live-badge';
    $('phaseNow').textContent=`尚未过零 · 已 ${fmt(p.elapsed_s,0)} s`;
    $('phaseDetail').textContent=
      `电流还在还原侧（受 offset 天花板约束那一侧），末 20 s 漂移 ${driftTxt}。`+
      `等它过零再切档 —— 现在切到 9 nA 会立刻撞轨、丢掉电位控制。`;
  } else {
    const ok=Boolean(p.ready);
    $('phaseBadge').textContent=ok?'氧化稳态 · 可切档':'氧化稳态 · 未稳定';
    $('phaseBadge').className=`live-badge ${ok?'running':''}`;
    $('phaseNow').textContent=
      `过零于 t = ${fmt(p.crossed_at_s,1)} s（已 ${fmt(p.since_cross_s,0)} s）`;
    $('phaseDetail').textContent=
      `末 20 s 漂移 ${driftTxt}（建议 ≤ ${fmt(p.drift_threshold_pa_s,0)} pA/s）`+
      (p.window_railed?` · ⚠️ 末窗仍有 ${p.window_railed} 个撞轨样本`:'')+
      (p.railed_frac>0.02?` · ⚠️ 本轮 ${fmt(p.railed_frac*100,0)}% 样本撞过轨，那部分电位是错的`:'');
  }
  $('switchMeasRange').disabled=!running||p.phase!=='oxidation'||atTarget;
  $('switchMeasRange').textContent=atTarget
    ? '已在测量档（250 nA / offset 9 nA）'
    : '切到测量档（250 nA / offset 9 nA）';
  if($('autoSwitchRange')!==document.activeElement) $('autoSwitchRange').checked=Boolean(auto.enabled);
}
function isMeasRange(a){ return Boolean(a)&&Number(a.fsr_pa)===250000&&Number(a.off_pa)===9000 }
$('switchMeasRange').onclick=async()=>{
  try{
    $('phaseError').hidden=true; $('switchMeasRange').disabled=true;
    const r=await post('/api/range/measurement',{});
    toast(`已下发 ${r.sent}`);
    updateMeasurement(await api('/api/status'));
  }catch(e){ errorBox('phaseError', e); }
};
$('autoSwitchRange').onchange=async()=>{
  try{
    $('phaseError').hidden=true;
    await post('/api/range/auto',{enabled:$('autoSwitchRange').checked});
  }catch(e){ errorBox('phaseError', e); $('autoSwitchRange').checked=!$('autoSwitchRange').checked }
};
$('applyRange').onclick=async()=>{
  try{
    $('rangeError').hidden=true; $('applyRange').disabled=true;
    const body={fsr_code:Number($('rangeFsr').value), offset_sel:Number($('rangeOff').value)};
    const r=await post('/api/range', body);
    toast(`已下发 ${r.sent}`);
    updateMeasurement(await api('/api/status'));
  }catch(e){ errorBox('rangeError', e); }
  finally{ $('applyRange').disabled=state.measurement?.state!=='running'; }
};

function syncChartWindowRun(runId){
  const nextRunId=String(runId||'');
  if(!nextRunId||nextRunId===state.chartRunId)return;
  state.chartRunId=nextRunId;
  if(state.chartWindowFixed)state.chartWindowS=300;
}
const METRIC_TREND_TOOLTIP='平稳仅代表斜率进入阈值，不代表自动停止全部门禁通过';
const METRIC_TRENDS={
  rising:['↑','上升'],falling:['↓','下降'],flat:['✓','平稳'],insufficient:['·','数据不足'],
};
function setMetricNote(id,text){const node=$(id);node.textContent=text;node.hidden=!text}
function metricCount(value){
  if(value===null||value===undefined||value==='')return '--';
  const number=Number(value);return Number.isFinite(number)?String(number):'--';
}
function renderItMetricStrip(data,running){
  const metrics=data.rolling_metrics||{},status=String(metrics.status||'idle');
  const ready=['ready','complete','frozen'].includes(status),accumulating=running&&!ready;
  $('metricPrimaryLabel').textContent='稳态电流';$('metricPrimaryUnit').textContent='nA';
  $('metricSecondaryLabel').textContent='噪声水平';$('metricSecondaryUnit').textContent='nA';
  $('steadyCurrent').textContent=ready?fmt(metrics.steady_current_nA,3):'--';
  $('steadySd').textContent=ready?fmt(metrics.noise_nA,3):'--';
  const metricNote=accumulating?'累积中':status==='idle'?'等待测量':'';
  setMetricNote('metricPrimaryNote',metricNote);setMetricNote('metricSecondaryNote',metricNote);

  $('metricTertiaryLabel').textContent='斜率';$('metricTertiaryUnit').textContent='nA/s';
  $('metricTertiaryValue').textContent=ready?fmt(metrics.slope_nA_per_s,3):'--';
  const reportedTrend=String(metrics.trend_state||'insufficient');
  const trendState=Object.prototype.hasOwnProperty.call(METRIC_TRENDS,reportedTrend)?reportedTrend:'insufficient';
  const trend=METRIC_TRENDS[trendState],trendLabel=trendState==='insufficient'
    ?(accumulating?'累积中':status==='idle'?'等待测量':trend[1]):trend[1];
  const trendNode=$('metricTrendState');
  trendNode.hidden=false;trendNode.textContent=`${trend[0]} ${trendLabel}`;
  trendNode.className=`metric-note metric-trend trend-${trendState}`;trendNode.title=METRIC_TREND_TOOLTIP;

  const pointText=`${metricCount(metrics.native_point_count)} 个原生点`;
  const adaptive=Boolean(data.settings?.adaptive_stop);
  const etaText=String(data.stability_eta?.display_text??'--');
  $('metricQuaternaryLabel').textContent=adaptive?'预计稳定':'进度';
  $('metricQuaternaryValue').textContent=adaptive
    ?(etaText==='曲线变化，正在重新估计'?'重新估计':etaText)
    :(metrics.progress_percent==null?'--':fmt(metrics.progress_percent,0));
  $('metricQuaternaryUnit').textContent=adaptive?'':'%';
  $('metricProgressDetail').textContent=adaptive&&etaText==='曲线变化，正在重新估计'
    ?`曲线变化 · ${pointText}`:pointText;
  $('metricProgressDetail').title=adaptive?etaText:'';
  $('metricProgressDetail').hidden=false;
}
function renderCvMetricStrip(data){
  const metrics=data.rolling_metrics||{},latest=data.latest_sample;
  $('metricPrimaryLabel').textContent='实时电位';$('metricPrimaryUnit').textContent='V';
  $('metricSecondaryLabel').textContent='当前循环';$('metricSecondaryUnit').textContent='圈';
  $('steadyCurrent').textContent=latest?fmt(latest.potential_v,3):'--';
  $('steadySd').textContent=latest?String(latest.cycle||'--'):'--';
  setMetricNote('metricPrimaryNote','');setMetricNote('metricSecondaryNote','');
  $('metricTertiaryLabel').textContent='原生点数';$('metricTertiaryValue').textContent=metricCount(metrics.native_point_count);
  const expectedNative=metricCount(metrics.expected_native_point_count);
  $('metricTertiaryUnit').textContent=expectedNative==='--'?'/ --':`/ ≈ ${expectedNative}`;$('metricTrendState').hidden=true;
  $('metricQuaternaryLabel').textContent='进度';
  $('metricQuaternaryValue').textContent=metrics.progress_percent==null?'--':fmt(metrics.progress_percent,0);
  $('metricQuaternaryUnit').textContent='%';$('metricProgressDetail').hidden=true;
}
function updateMeasurement(data){
  syncChartWindowRun(data.run_id);
  state.measurement=data; const running=data.state==='running', complete=data.state==='completed';
  setPlateauFormalRunLock(running&&!Boolean(data.metadata?.debug));
  renderRange(data); renderTransient(data); renderCellV(data);
  $('measureMessage').textContent=data.message||''; $('liveBadge').textContent=running?'采集中':complete?'已完成':data.state==='error'?'错误':'待机'; $('liveBadge').className=`live-badge ${running?'running':data.state==='error'?'error':''}`;
  $('stopMeasure').disabled=!running; $('useForCalibration').disabled=!complete||data.summary?.steady_current_nA==null; $('predictConcentration').disabled=!complete||data.summary?.steady_current_nA==null;
  const cv=(data.settings?.method||state.method)==='cv';
  if(cv)renderCvMetricStrip(data);else renderItMetricStrip(data,running);
  const displaySeries=filterValues(data.data?.time_s||[],data.data?.current_nA||[],data.data?.valid).values;
  const live=data.latest_sample;
  const displayLive=live&&displaySeries.length?{...live,current_nA:displaySeries[displaySeries.length-1]}:live;
  $('liveCurrent').textContent=displayLive?fmt(cv?displayLive.current_nA/1000:displayLive.current_nA,3):'--';
  $('liveCurrentUnit').textContent=cv?'µA':'nA';
  $('liveCurrentTime').textContent=live?(cv?`${fmt(live.potential_v,3)} V · 第 ${live.cycle} 圈 · 点 ${Number(live.index)+1}`:`t = ${fmt(live.time_s,2)} s · 点 ${Number(live.index)+1}`):'尚无数据';
  $('liveCurrentBox').classList.toggle('invalid',Boolean(displayLive&&!displayLive.valid));
  const hardwareState=data.state==='error'?'error':running?'busy':data.state==='completed'?'ok':'';
  $('deviceDot').className=`status-dot ${hardwareState}`;
  $('deviceState').textContent=data.state==='error'?'硬件 / 采集异常':running?'设备测量中':data.state==='completed'?'上轮测量完成':'硬件待测';
  const transportLabel=String(data.transport_label||(
    data.transport==='serial'?'USB DATA CDC':data.transport==='rtt'?'RTT / J-Link':'连接方式未知'
  ));
  const deviceName=String(data.device_name||state.devices?.selected_device?.name||'');
  $('deviceTransport').textContent=`${deviceName||transportLabel} · MAX30131`;
  if(data.error){$('measureError').textContent=data.error;$('measureError').hidden=false}else $('measureError').hidden=true; updateStartState();drawAll();void handleWorkflowCompletion(data);
}
async function refreshMeasurement(){if(state.exiting)return;try{updateMeasurement(await api('/api/status'))}catch(e){$('deviceState').textContent='服务未连接';$('deviceTransport').textContent='连接方式未知 · MAX30131';$('deviceDot').className='status-dot'}}
async function measurementRefreshLoop(){
  if(state.exiting)return;
  await refreshMeasurement();
  if(state.exiting)return;
  const running = state.measurement?.state === 'running';
  const duration = Number(state.measurement?.settings?.duration_s || 180);
  /* Long CV runs still render every native point; batching UI refreshes keeps
   * the 72,000-point status payload responsive without decimation. */
  setTimeout(measurementRefreshLoop, running ? (duration <= 300 ? 100 : 2000) : 1000);
}
$('exitApp').addEventListener('click',async()=>{
  if(state.exiting)return;
  const activeMeasurement=state.measurement?.state==='running';
  const activeSchedule=Boolean(state.schedule?.active);
  if((activeMeasurement||activeSchedule)&&!confirm('当前仍有硬件任务，退出会先停止任务并关闭后端。确定退出吗？'))return;
  state.exiting=true;
  const button=$('exitApp');
  button.disabled=true;button.textContent='退出中…';
  let acknowledged=false;
  try{await post('/api/shutdown');acknowledged=true}catch{}
  try{window.open('','_self');window.close()}catch{}
  setTimeout(()=>{
    if(window.closed)return;
    button.disabled=false;
    button.textContent=acknowledged?'后端已退出，请关闭标签页':'后端未响应，请检查运行进程';
  },350);
});
function deviceCardDetail(device){
  if(device.kind==='jlink')return `${device.transport_label||'RTT / J-Link'}${device.probe_serial?` · 探头 SN ${device.probe_serial}`:''}`;
  const ports=[device.data_port&&`DATA ${device.data_port}`,device.smp_port&&`SMP ${device.smp_port}`].filter(Boolean);
  if(device.probe_required)return '测量进行中，暂不打开 CDC 探测';
  return ports.length?ports.join(' · '):'未识别 DATA/SMP 接口';
}
function renderDeviceList(payload=state.devices){
  state.devices=payload||{devices:[],selected_device_id:null,busy:false};
  const devices=Array.isArray(state.devices.devices)?state.devices.devices:[];
  const selected=state.devices.selected_device_id;
  const list=$('deviceList'); list.replaceChildren();
  const auto=document.createElement('article'); auto.className=`device-card ${selected?'':'selected'}`;
  auto.innerHTML='<div><strong>自动检测</strong><small>只有一个设备时自动使用；多个设备时保留当前选择</small></div><button class="secondary" type="button" data-device-id="auto">使用自动检测</button>';
  const autoButton=auto.querySelector('button'); autoButton.disabled=Boolean(state.devices.busy)||!selected;
  list.append(auto);
  devices.forEach(device=>{
    const card=document.createElement('article'); card.className=`device-card ${device.id===selected?'selected':''}${device.selectable?'':' unavailable'}`;
    const title=document.createElement('div'); title.innerHTML=`<strong>${escapeHtml(device.name||'未命名设备')}</strong><small>${escapeHtml(deviceCardDetail(device))}</small>`;
    const action=document.createElement('button'); action.type='button'; action.className='secondary'; action.dataset.deviceId=device.id;
    action.textContent=device.id===selected?'当前使用':'选择'; action.disabled=Boolean(state.devices.busy)||device.id===selected||!device.selectable;
    card.append(title,action);
    const note=document.createElement('small'); note.className='device-state'; note.textContent=device.id===selected?'当前测量将使用此设备':device.selectable?'空闲时可选择':'设备尚未准备好'; card.append(note);
    list.append(card);
  });
  if(!devices.length){const empty=document.createElement('div');empty.className='device-empty';empty.textContent='没有发现可识别的 USB DATA 或 J-Link。';list.append(empty)}
  const count=devices.length?`${devices.length} 个设备`:'未发现设备';
  $('deviceDialogSummary').textContent=state.devices.busy?`${count} · 测量进行中，暂不能切换`:`${count} · 选择后会锁定该设备`;
  $('deviceDialogBusy').hidden=!state.devices.busy;
  $('deviceDialogBusy').textContent=state.devices.busy?'测量或自动任务运行期间不能切换设备':'';
}
async function refreshDevices(open=false){
  const dialog=$('deviceDialog');
  if(open){if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','')}
  try{renderDeviceList(await api('/api/devices'))}
  catch(e){$('deviceDialogSummary').textContent='设备列表读取失败';$('deviceDialogBusy').textContent=e.message;$('deviceDialogBusy').hidden=false}
}
async function chooseDevice(deviceId){
  const buttons=$('deviceList').querySelectorAll('button');buttons.forEach(button=>{button.disabled=true});
  try{
    const data=await post('/api/devices/select',{device_id:deviceId});
    renderDeviceList(data); await refreshMeasurement();
    const dialog=$('deviceDialog');if(typeof dialog.close==='function')dialog.close();else dialog.removeAttribute('open');
    toast(data.message||'设备选择已更新');
  }catch(e){buttons.forEach(button=>{button.disabled=false});$('deviceDialogBusy').textContent=e.message;$('deviceDialogBusy').hidden=false}
}
$('selectDevice').addEventListener('click',()=>void refreshDevices(true));
$('refreshDevices').addEventListener('click',()=>void refreshDevices(false));
$('deviceList').addEventListener('click',event=>{const button=event.target.closest('button[data-device-id]');if(button&&!button.disabled)void chooseDevice(button.dataset.deviceId)});
$('chartWindow').addEventListener('click', event => {
  const button = event.target.closest('button[data-window]');
  if (!button) return;
  state.chartWindowFixed = button.dataset.window === '300';
  state.chartWindowS = button.dataset.window === 'all' ? null : Number(button.dataset.window);
  $('chartWindow').querySelectorAll('button').forEach(node => node.classList.toggle('active', node === button));
  drawAll();
});
$('startMeasure').addEventListener('click',async()=>{const button=$('startMeasure'),label=button.textContent;try{$('measureError').hidden=true;button.disabled=true;button.textContent='正在核对硬件配置…';updateMeasurement(await post('/api/measurement/start',{sample_name:$('sampleName').value,known_concentration_um:concentrationValue(),sample_role:state.sampleRole,save_dir:$('saveDirectory').value,source:'manual_gui'}))}catch(e){errorBox('measureError',e)}finally{button.textContent=label;updateStartState()}});
$('stopMeasure').addEventListener('click',async()=>{try{updateMeasurement(await post('/api/measurement/stop'))}catch(e){errorBox('measureError',e)}});
$('sampleRole').addEventListener('click',event=>{const button=event.target.closest('button[data-role]');if(button)setSampleRole(button.dataset.role)});
$('sampleName').addEventListener('input',previewFilename);$('knownConcentration').addEventListener('input',previewFilename);
$('doubleKnownConcentration').addEventListener('click',()=>scaleKnownConcentration(2));
$('halveKnownConcentration').addEventListener('click',()=>scaleKnownConcentration(.5));
$('applySaveDirectory').onclick=async()=>{try{renderWorkflow(await post('/api/workflow/config',{save_dir:$('saveDirectory').value}));state.calibration=await api('/api/calibration');renderCalibration();toast('保存目录已应用')}catch(e){errorBox('measureError',e)}};
$('resetCalibration').onclick=async()=>{if(!confirm('开始一套新标定？软件会在当前工作区下新建一个批次目录，旧批次数据保持不变。'))return;try{renderWorkflow(await post('/api/workflow/reset-calibration'));state.calibration=await api('/api/calibration');renderCalibration();setSampleRole('calibration',true);toast('已新建批次并切换到新目录')}catch(e){errorBox('measureError',e)}};

function readSettings(){const potential=Number($('potentialV').value),low=Number($('cvLowV').value),high=Number($('cvHighV').value),rate=Number($('cvScanRate').value),cycles=Number($('cvCycles').value),cv=state.method==='cv',duration=cv?2*(high-low)/rate*cycles:Number($('durationS').value);return {method:state.method,initial_potential_v:cv?low:potential,potential_v:cv?low:potential,working_electrode_v:Number($('workingElectrodeV').value),prestep_s:cv?Number($('cvQuietS').value):0,duration_s:duration,adaptive_stop:!cv&&$('adaptiveStop').checked,sens_period_code:Number($('sensPeriodCode').value),target_rate_hz:Number($('sampleRateHz').value),fit_window_s:Number($('fitWindowS').value),fsr_nA:Number($('fsrNA').value),offset_mode:$('offsetNA').value,cv_low_v:low,cv_high_v:high,cv_scan_rate_v_s:rate,cv_cycles:cycles,cv_step_v:.001,cv_quiet_s:Number($('cvQuietS').value),cv_eis_fsr_uA:Number($('cvEisFsrUA').value)}}
function renderOffsetLabels(fsr){$('offsetNA').querySelectorAll('option[data-pct]').forEach(option=>{const pct=Number(option.dataset.pct);option.textContent=`${pct}% FSR (${fsr*pct/100} nA)`})}
function signedPotential(value){const number=Number(value);return `${number>0?'+':''}${number.toFixed(2)}`}
function renderSettings(data){
  state.settings=data; const s=data.settings, periods=[124,242,476,945,1882,3757];
  if(!state.measureControlInitialized){setMeasureControlTab(data.applied?'sample':'settings');state.measureControlInitialized=true}
  state.method=s.method||'it';const cv=state.method==='cv',adaptive=!cv&&Boolean(s.adaptive_stop),wideIt=!cv&&s.fsr_nA>2000;
  const nativeRate=cv?s.cv_scan_rate_v_s/s.cv_step_v:wideIt?s.target_rate_hz:1000/periods[s.sens_period_code??0];
  $('workingElectrodeV').value=s.working_electrode_v??1.2;if(!cv){$('potentialV').value=s.potential_v;$('durationS').value=s.duration_s;$('adaptiveStop').checked=adaptive} $('durationField').hidden=false;$('fitWindowField').hidden=false;$('durationS').disabled=adaptive;$('fitWindowS').disabled=false;$('durationField').classList.toggle('disabled',adaptive);$('fitWindowField').classList.remove('disabled');$('sensPeriodCode').value=String(s.sens_period_code??0); $('sampleRateHz').value=s.target_rate_hz; $('fitWindowS').value=s.fit_window_s; $('fsrNA').value=String(s.fsr_nA);renderOffsetLabels(s.fsr_nA);$('offsetNA').value=s.offset_mode||`${s.offset_nA??19}nA`;
  $('cvLowV').value=s.cv_low_v;$('cvHighV').value=s.cv_high_v;$('cvScanRate').value=s.cv_scan_rate_v_s;$('cvCycles').value=s.cv_cycles;$('cvQuietS').value=s.cv_quiet_s;$('cvEisFsrUA').value=String(s.cv_eis_fsr_uA??20);
  $('methodMode').querySelectorAll('button').forEach(button=>button.classList.toggle('active',button.dataset.method===state.method));$('itSettings').hidden=cv;$('cvSettings').hidden=!cv;$('sampleRoleBlock').hidden=cv;$('concentrationField').hidden=cv;$('dcSamplingField').hidden=cv||wideIt;$('dcRangeField').hidden=cv;$('dcOffsetField').hidden=cv||wideIt;
  $('chartTitle').textContent=cv?'CV 循环伏安曲线':'I-T 计时电流曲线';$('settingsTitle').textContent=cv?'CV 条件':'I-T 条件';
  $('itChart').setAttribute('aria-label',cv?'CV 电位电流曲线':'I-T 电流时间曲线');
  if(cv)renderCvMetricStrip({settings:s,rolling_metrics:{status:'idle'}});
  else renderItMetricStrip({settings:s,rolling_metrics:{status:'idle'},stability_eta:{display_text:'--'}},false);
  const points=cv?Math.round(s.duration_s*nativeRate):Math.round(s.duration_s*s.target_rate_hz);
  $('outputPoints').textContent=cv?`${s.cv_cycles} 圈 · ${s.cv_cycles*2} 段 · 约 ${points} 个原生电流点`:adaptive?`持续采集 · 原生 ${fmt(nativeRate,2)} Hz`:`${points} 点 · 原生 ${fmt(nativeRate,2)} Hz`;
  $('outputPoints').nextElementSibling.textContent=cv?'波形按 1 mV 步进；每个 EIS ADC 原生电流点实时显示并保存':wideIt?'宽量程 I-T 使用 EIS ADC；单次电位扰动小于 0.4 mV':`MAX30131 原生约 ${fmt(nativeRate,2)} Hz；更高输出频率由时间戳重采样生成`;
  pages.measure[1]=cv?`${signedPotential(s.cv_low_v)} 至 ${signedPotential(s.cv_high_v)} V · ${s.cv_scan_rate_v_s} V/s · ${s.cv_cycles} 圈`:adaptive?`I-T 智能平台检测与末 ${s.fit_window_s} 秒稳态分析`:`${s.duration_s} 秒 I-T 检测与末 ${s.fit_window_s} 秒稳态分析`; if($('view-measure').classList.contains('active'))$('pageSubtitle').textContent=pages.measure[1];
  const settingsFailed=data.state==='error', failDetail=String(data.error||'').trim();
  $('settingsMessage').textContent=settingsFailed&&failDetail?failDetail:data.message; $('settingsMessage').title=settingsFailed&&failDetail?failDetail:''; $('settingsMessage').classList.toggle('error-text',settingsFailed&&!!failDetail);
  $('settingsBadge').textContent=data.state==='applying'?'应用中':settingsFailed?'失败':data.applied?'已应用':'未应用'; $('settingsBadge').className=`live-badge ${settingsFailed?'error':data.applied?'running':''}`;
  const itCommonMode=`V_WE ${fmt(s.working_electrode_v,3)} V · V_RE ${fmt(s.working_electrode_v-s.potential_v,3)} V`;
  $('firmwareNote').textContent=cv?`CV ${signedPotential(s.cv_low_v)}–${signedPotential(s.cv_high_v)} V · EIS ADC ${s.cv_eis_fsr_uA} µA`:wideIt?`${signedPotential(s.potential_v)} V 恒电位 · ${itCommonMode} · EIS ADC ${s.fsr_nA/1000} µA`:`${signedPotential(s.potential_v)} V 恒电位 · ${itCommonMode} · ${s.fsr_nA} nA FSR`; $('scheduleMethodLabel').textContent=cv?`CV · ${s.cv_cycles} 圈 · ${Math.round(s.duration_s/60)} 分钟`:adaptive?`恒电位 I-T · 智能停止 · ${signedPotential(s.potential_v)} V`:`恒电位 I-T · ${s.duration_s} 秒 · ${signedPotential(s.potential_v)} V`;
  $('chartWindow').querySelectorAll('button').forEach((button,index)=>{if(cv){const labels=['全程','5 圈','1 圈'];const values=['all','5','1'];button.textContent=labels[index];button.dataset.window=values[index]}else{const labels=['300 s','20 s','5 s'];const values=['300','20','5'];button.textContent=labels[index];button.dataset.window=values[index]}});
  state.chartWindowS=cv?null:300;state.chartWindowFixed=!cv;$('chartWindow').querySelectorAll('button').forEach((button,index)=>button.classList.toggle('active',index===0));
  const plateau=state.plateau?.settings||PLATEAU_DEFAULTS;
  const adaptiveWindow=Math.max(plateauWindowDuration(plateau),Number(s.fit_window_s||0));
  const adaptiveMinimum=Number(s.prestep_s||0)+adaptiveWindow
    +(Math.max(1,Number(plateau.required_consecutive_windows||1))-1)*Number(plateau.segment_duration_s||5)+10;
  const minInterval=adaptive?adaptiveMinimum/60:(s.prestep_s+s.duration_s+10)/60; $('intervalMinutes').min=minInterval.toFixed(2); if(Number($('intervalMinutes').value)<minInterval)$('intervalMinutes').value=Math.ceil(minInterval*4)/4;
  renderWorkflow(state.workflow||{save_dir:'',stage:'collect',calibration_ready:false,points_count:0,selected_points_count:0,settings_match:true});renderScheduleMode();drawAll();
  updateStartState(); $('startSchedule').disabled=state.schedule?.active||!data.applied;
}
function settingsChanged(){if(!state.settings)return;state.settingsDirty=true;state.settings.applied=false;state.settings.state='not_applied';state.settings.message='参数已修改，请重新应用';const s=readSettings();state.settings.settings=s;renderSettings(state.settings)}
['potentialV','workingElectrodeV','durationS','adaptiveStop','sensPeriodCode','sampleRateHz','fitWindowS','fsrNA','offsetNA','cvLowV','cvHighV','cvScanRate','cvCycles','cvQuietS','cvEisFsrUA'].forEach(id=>$(id).addEventListener('change',settingsChanged));
$('methodMode').addEventListener('click',event=>{const button=event.target.closest('button[data-method]');if(!button||button.dataset.method===state.method)return;state.method=button.dataset.method;settingsChanged()});
$('applySettings').onclick=async()=>{try{$('applySettings').disabled=true;$('applySettings').textContent='正在编译并烧录…';renderSettings({...state.settings,settings:readSettings(),state:'applying',message:'正在编译并写入硬件参数',applied:false});const data=await post('/api/settings/apply',readSettings());state.settingsDirty=false;renderSettings(data);renderWorkflow(await api('/api/workflow'));toast(`${state.method.toUpperCase()} 条件已应用到硬件`)}catch(e){errorBox('measureError',e);try{state.settingsDirty=false;renderSettings(await api('/api/settings'))}catch{}}finally{$('applySettings').disabled=false;$('applySettings').textContent='应用条件并烧录硬件'}};

function makeCell(tr,content){const td=document.createElement('td');if(content instanceof Node)td.appendChild(content);else td.textContent=content;tr.appendChild(td);return td}
function pointInput(key,value,type='text'){const input=document.createElement('input');input.dataset.k=key;input.type=type;input.value=value??'';if(type==='number')input.step='0.001';return input}
function row(point={},index=0){
  const tr=document.createElement('tr');tr.dataset.pointId=point.point_id||`manual-${Date.now()}-${index}`;tr.dataset.acquiredAt=point.acquired_at||0;tr.dataset.runId=point.run_id||'';tr.dataset.dataPath=point.data_path||'';
  const use=document.createElement('input');use.type='checkbox';use.className='point-selector';use.checked=Boolean(point.selected);use.addEventListener('change',syncCalibrationPreview);makeCell(tr,use);
  makeCell(tr,String(index+1));
  makeCell(tr,point.acquired_at?new Date(Number(point.acquired_at)*1000).toLocaleString('zh-CN',{hour12:false}):'手动');
  const labelInput=pointInput('label',point.label||'');labelInput.addEventListener('input',()=>{refreshRangeControls();syncCalibrationPreview()});makeCell(tr,labelInput);const concentrationInput=pointInput('concentration_um',point.concentration_um,'number'),currentInput=pointInput('current_nA',point.current_nA,'number');concentrationInput.addEventListener('input',syncCalibrationPreview);currentInput.addEventListener('input',syncCalibrationPreview);makeCell(tr,concentrationInput);makeCell(tr,currentInput);
  const remove=document.createElement('button');remove.className='delete-point';remove.title='删除候选点';remove.textContent='×';remove.onclick=()=>{tr.remove();refreshRangeControls();syncCalibrationPreview()};makeCell(tr,remove);return tr;
}
function refreshRangeControls(){
  const rows=[...$('pointsBody').querySelectorAll('tr')],start=$('rangeStart'),end=$('rangeEnd'),oldStart=start.value,oldEnd=end.value;start.innerHTML='';end.innerHTML='';
  rows.forEach((tr,index)=>{const label=tr.querySelector('[data-k="label"]')?.value||`点 ${index+1}`;[start,end].forEach(select=>{const option=document.createElement('option');option.value=String(index);option.textContent=`#${index+1} ${label}`;select.appendChild(option)})});
  start.disabled=end.disabled=rows.length===0;$('applyPointRange').disabled=rows.length===0;if(rows.length){start.value=oldStart&&Number(oldStart)<rows.length?oldStart:'0';end.value=oldEnd&&Number(oldEnd)<rows.length?oldEnd:String(rows.length-1)}updateSelectedCount();
}
function renderPoints(points){const body=$('pointsBody');body.innerHTML='';(points||[]).forEach((p,index)=>body.appendChild(row(p,index)));refreshRangeControls()}
function readPoints(){return [...$('pointsBody').querySelectorAll('tr')].map(tr=>({point_id:tr.dataset.pointId,acquired_at:Number(tr.dataset.acquiredAt||0),run_id:tr.dataset.runId,data_path:tr.dataset.dataPath,label:tr.querySelector('[data-k="label"]').value,concentration_um:tr.querySelector('[data-k="concentration_um"]').value,current_nA:tr.querySelector('[data-k="current_nA"]').value,selected:tr.querySelector('.point-selector').checked})).filter(p=>p.concentration_um!==''&&p.current_nA!=='').map(p=>({...p,concentration_um:Number(p.concentration_um),current_nA:Number(p.current_nA)}))}
function updateSelectedCount(){const rows=[...$('pointsBody').querySelectorAll('tr')],selected=rows.filter(tr=>tr.querySelector('.point-selector').checked).length;$('selectedCount').textContent=`已选 ${selected} / ${rows.length}`}
function syncCalibrationPreview(){state.calibrationDirty=true;updateSelectedCount();state.calibration.points=readPoints();drawAll()}
$('addPoint').onclick=()=>{$('pointsBody').appendChild(row({},$('pointsBody').children.length));refreshRangeControls();syncCalibrationPreview()};
$('applyPointRange').onclick=()=>{const rows=[...$('pointsBody').querySelectorAll('tr')],a=Math.min(Number($('rangeStart').value),Number($('rangeEnd').value)),b=Math.max(Number($('rangeStart').value),Number($('rangeEnd').value));rows.forEach((tr,index)=>tr.querySelector('.point-selector').checked=index>=a&&index<=b);syncCalibrationPreview()};
$('clearPointSelection').onclick=()=>{$('pointsBody').querySelectorAll('.point-selector').forEach(input=>input.checked=false);syncCalibrationPreview()};
$('useForCalibration').onclick=()=>{const current=state.measurement?.summary?.steady_current_nA, concentration=$('knownConcentration').value;if(concentration===''){toast('请先填写已知浓度');return}$('pointsBody').appendChild(row({label:$('sampleName').value||state.measurement.run_id,concentration_um:concentration,current_nA:current}));document.querySelector('[data-view="calibrate"]').click();toast('已加入标定数据')};
$('predictConcentration').onclick=async()=>{try{const result=await post('/api/predict',{});$('predictionResult').querySelector('strong').textContent=fmt(result.predicted_concentration_um,3);toast('浓度预测完成')}catch(e){errorBox('measureError',e)}};
$('fitCalibration').onclick=async()=>{try{const points=readPoints(),selected=points.filter(point=>point.selected).map(point=>point.point_id);if(!selected.length){toast('请先选择一个标定点范围');return}const data=await post('/api/calibration/fit',{points,points_revision:state.calibration?.points_revision??null,selected_point_ids:selected,degree:Number($('fitDegree').value)});state.calibration=data;renderCalibration();renderWorkflow(await api('/api/workflow'));setSampleRole('test',true);toast('选中范围已生成并锁定为测试曲线')}catch(e){toast(e.message)}};
function modelCurrentAt(model, concentration){
  const coefficients=(model?.coefficients||[]).map(Number), x=Number(concentration);
  if(!coefficients.length||!Number.isFinite(x))return null;
  const degree=Number(model.degree), value=degree===1
    ? coefficients[0]*x+coefficients[1]
    : coefficients.reduce((sum, coefficient)=>sum*x+coefficient,0);
  return Number.isFinite(value)?value:null;
}
function modelPredictAt(model, current){
  const coefficients=(model?.coefficients||[]).map(Number), y=Number(current);
  if(!coefficients.length||!Number.isFinite(y))return null;
  const degree=Number(model.degree), lo=Number(model.concentration_min_um), hi=Number(model.concentration_max_um);
  if(degree===1){if(Math.abs(coefficients[0])<1e-15)return null;return (y-coefficients[1])/coefficients[0]}
  if(degree!==2||coefficients.length<3)return null;
  const a=coefficients[0], b=coefficients[1], c=coefficients[2]-y, discriminant=b*b-4*a*c;
  let roots;
  if(Math.abs(a)<1e-15){if(Math.abs(b)<1e-15)return null;roots=[-c/b]}
  else{if(discriminant<0)return null;roots=[(-b+Math.sqrt(discriminant))/(2*a),(-b-Math.sqrt(discriminant))/(2*a)]}
  const centre=(lo+hi)/2,candidates=roots.filter(root=>root>=lo-1e-9&&root<=hi+1e-9);
  return candidates.length?candidates.sort((left,right)=>(Math.abs(left-centre)-Math.abs(right-centre))||(right-left))[0]:null;
}
function localApDetail(trueValue, measuredValue){
  const x=Number(trueValue); if(trueValue===null||trueValue===''||!Number.isFinite(x)||x<0)return null;
  if(measuredValue===null||measuredValue===''||!Number.isFinite(Number(measuredValue)))return {zone:'grey',score:0,absolute_error_um:null,error_percent:null,missing:true};
  const y=Number(measuredValue);
  const error=Math.abs(y-x), percent=x===0?null:error/x*100, blueLimit=x<10?2:.2, greenLimit=x<10?4:.4;
  const blue=x<10?error<=blueLimit:error/x<=blueLimit, green=x<10?Math.max(0,x-4)<=y&&y<=x+4:.6*x<=y&&y<=1.4*x;
  const distance=x<10?error:error/x;
  return {zone:blue?'blue':green?'green':'grey',score:blue?1:green?Math.max(0,Math.min(1,1-(distance-blueLimit)/(greenLimit-blueLimit))):0,absolute_error_um:error,error_percent:percent};
}
function localApScore(points){
  const prepared=(points||[]).map((point,index)=>{const detail=localApDetail(point.concentration_um,point.predicted_concentration_um);return detail?{sequence:index+1,concentration_um:Number(point.concentration_um),measured_concentration_um:point.predicted_concentration_um!==null&&point.predicted_concentration_um!==''&&Number.isFinite(Number(point.predicted_concentration_um))?Number(point.predicted_concentration_um):null,...detail}:null}).filter(Boolean), scoring=prepared.slice(0,24);
  let streak=0,longest=0; scoring.forEach(point=>{const weight=point.zone==='blue'?1:point.zone==='green'?.5:0;if(!weight)streak=0;else{streak+=weight;longest=Math.max(longest,streak)}});
  const thresholds=[[24,10],[21,8],[18,6],[15,4.5],[12,3],[10,2],[8,1.5],[6,1],[4,.5]], sc=(thresholds.find(([limit])=>longest>=limit)||[0,0])[1], scoreSum=scoring.reduce((sum,point)=>sum+point.score,0), abs=prepared.map(point=>point.absolute_error_um).filter(Number.isFinite), percentages=prepared.map(point=>point.error_percent).filter(Number.isFinite);
  const signed=prepared.filter(point=>Number.isFinite(point.measured_concentration_um)).map(point=>point.measured_concentration_um-point.concentration_um), stats={measured_count:prepared.length,scored_count:scoring.length,blue_count:prepared.filter(point=>point.zone==='blue').length,green_count:prepared.filter(point=>point.zone==='green').length,grey_count:prepared.filter(point=>point.zone==='grey').length,mean_absolute_error_um:abs.length?abs.reduce((a,b)=>a+b,0)/abs.length:null,rmse_um:abs.length?Math.sqrt(abs.reduce((a,b)=>a+b*b,0)/abs.length):null,mean_absolute_error_percent:percentages.length?percentages.reduce((a,b)=>a+b,0)/percentages.length:null,max_absolute_error_um:abs.length?Math.max(...abs):null,max_absolute_error_percent:percentages.length?Math.max(...percentages):null,mean_signed_error_um:signed.length?signed.reduce((a,b)=>a+b,0)/signed.length:null};
  const ms=10*scoreSum/24; return {sample_count:24,points:prepared,stats,longest_weighted_streak:longest,ms,sc,final_score:100+5*(ms+sc)};
}
function hasUninvertibleApPoint(score){return Boolean(score?.points?.some(point=>point.measured_concentration_um===null))}
function validationPointFromRow(tr){
  const model=state.calibration?.model, bias=Number(state.calibration?.drift_bias_nA||0), concentrationRaw=tr.querySelector('[data-validation-key="concentration_um"]')?.value?.trim()||'', currentRaw=tr.querySelector('[data-validation-key="current_nA"]')?.value?.trim()||'', concentration=concentrationRaw===''?null:Number(concentrationRaw), current=currentRaw===''?null:Number(currentRaw), predicted=model&&Number.isFinite(current)?modelPredictAt(model,current-bias):null, expected=model&&Number.isFinite(concentration)?modelCurrentAt(model,concentration)+bias:null;
  return {point_id:tr.dataset.pointId||'',sample_name:tr.querySelector('[data-validation-key="sample_name"]')?.value||'',concentration_um:concentration,current_nA:current,predicted_concentration_um:predicted,expected_current_nA:expected,error_nA:Number.isFinite(current)&&Number.isFinite(expected)?current-expected:null};
}
function syncValidationRow(tr){
  const point=validationPointFromRow(tr), detail=localApDetail(point.concentration_um,point.predicted_concentration_um), cells=tr.querySelectorAll('[data-validation-derived]');
  const zoneLabel={blue:'Blue · 满分',green:'Green · 部分',grey:'Grey · 0 分'};
  const values=[point.expected_current_nA,point.error_nA,point.predicted_concentration_um,detail?.absolute_error_um,detail?.error_percent,zoneLabel[detail?.zone]||'--',detail?.score];
  cells.forEach((cell,index)=>{cell.textContent=index===4?fmt(values[index],2):index===5?String(values[index]):fmt(values[index],3);cell.className=`validation-derived ${detail?.zone?`zone-${detail.zone}`:''}`});
  tr.dataset.zone=detail?.zone||'';tr.dataset.predicted=Number.isFinite(point.predicted_concentration_um)?String(point.predicted_concentration_um):'';return {...point,...(detail||{})};
}
function readValidationPoints(){return [...$('validationBody').querySelectorAll('tr[data-point-id]')].map(syncValidationRow)}
function renderApMetrics(score){
  const stats=score?.stats||{};$('apFinalBadge').textContent=score?`Final ${fmt(score.final_score,1)} / 200`:'Final -- / 200';$('apMs').textContent=score?fmt(score.ms,2):'--';$('apSc').textContent=score?fmt(score.sc,2):'--';$('apStreak').textContent=score?fmt(score.longest_weighted_streak,1):'--';$('apMeasured').textContent=stats.measured_count??'--';$('apZones').textContent=score?`${stats.blue_count||0} / ${stats.green_count||0} / ${stats.grey_count||0}`:'--';$('apMae').textContent=fmt(stats.mean_absolute_error_um,3);$('apMape').textContent=fmt(stats.mean_absolute_error_percent,2);$('apRmse').textContent=fmt(stats.rmse_um,3);
}
function updateValidationSummary(){
  const points=readValidationPoints(), score=localApScore(points);
  state.calibration.validation_points=points;state.calibration.ap_score=score;renderApMetrics(score);
  const currentErrors=points.map(point=>point.error_nA).filter(Number.isFinite);
  const currentAbs=currentErrors.map(Math.abs);
  const currentMae=currentAbs.length?currentAbs.reduce((sum,value)=>sum+value,0)/currentAbs.length:null;
  const text=score.stats.measured_count
    ? `已测 ${score.stats.measured_count} 个测试点 · 平均偏差 ${fmt(score.stats.mean_signed_error_um,3)} µM · 平均绝对误差 ${fmt(score.stats.mean_absolute_error_um,3)} µM · 平均误差百分比 ${fmt(score.stats.mean_absolute_error_percent,2)}% · 电流误差 MAE ${fmt(currentMae,3)} nA · 最大电流偏差 ${fmt(currentAbs.length?Math.max(...currentAbs):null,3)} nA${hasUninvertibleApPoint(score)?' · 含无法反解点':''}`
    : '暂无测试点';
  $('validationSummary').textContent=text;drawAll();
}
function renderValidation(points){
  const body=$('validationBody');body.replaceChildren();const rows=state.calibration?.model?(points||[]):[];$('validationEmpty').hidden=rows.length>0;
  rows.forEach((point,index)=>{const tr=document.createElement('tr');tr.dataset.pointId=point.point_id||point.run_id||`validation-${index+1}`;const number=document.createElement('td');number.textContent=String(index+1);tr.appendChild(number);[['sample_name',point.sample_name||''],['concentration_um',point.concentration_um],['current_nA',point.current_nA]].forEach(([key,value])=>{const td=document.createElement('td'),input=document.createElement('input');input.dataset.validationKey=key;input.value=value??'';if(key!=='sample_name'){input.type='number';input.step='0.001';input.min=key==='concentration_um'?'0':''}input.addEventListener('input',()=>{state.validationDirty=true;$('validationEditBadge').textContent='未保存修改';$('validationEditBadge').className='live-badge warn';$('saveValidation').disabled=false;updateValidationSummary()});td.appendChild(input);tr.appendChild(td)});for(let i=0;i<7;i++){const td=document.createElement('td');td.dataset.validationDerived='true';tr.appendChild(td)}body.appendChild(tr);syncValidationRow(tr)});
  state.validationDirty=false;$('validationEditBadge').textContent='已保存';$('validationEditBadge').className='live-badge running';$('saveValidation').disabled=true;updateValidationSummary();
}
function renderCalibration(){
  const c=state.calibration||{}, {model,points,validation_points=[],model_path,model_created_at,drift_bias_nA,model_compatible}=c;
  if(points)renderPoints(points);$('modelR2').textContent=model?fmt(model.r2,4):'--';$('modelRmse').textContent=model?`${fmt(model.rmse_nA,2)} nA`:'--';$('modelSlope').textContent=model&&model.degree===1?`${fmt(model.coefficients[0],3)} nA/µM`:'--';const bias=Number(drift_bias_nA||0);$('calibrationStatus').textContent=model&&!model_compatible?'旧条件曲线 · 当前 IT 条件不匹配，测试已禁用':model?`已锁定 ${model.n_points} 个选中点 · ${model.concentration_min_um}–${model.concentration_max_um} µM${bias?` · bias ${bias>0?'+':''}${fmt(bias,3)} nA`:''}`:'选择至少两个不同浓度的候选点';$('modelPath').textContent=model_path?`${model_path}${model_created_at?` · ${new Date(model_created_at*1000).toLocaleString('zh-CN',{hour12:false})}`:''}`:'尚未生成测试曲线';renderValidation(validation_points);state.calibrationDirty=false;drawAll();
}
$('saveValidation').onclick=async()=>{try{$('saveValidation').disabled=true;const data=await post('/api/calibration/validation',{points:readValidationPoints().map(point=>({point_id:point.point_id,sample_name:point.sample_name,concentration_um:point.concentration_um,current_nA:point.current_nA}))});state.calibration=data;state.validationDirty=false;renderCalibration();toast('测试点修改已保存')}catch(e){$('saveValidation').disabled=false;toast(e.message)}};

function driftOption(record){const date=new Date(record.finished_at*1000).toLocaleString('zh-CN',{hour12:false});return `${date} · ${fmt(record.steady_current_nA,3)} nA · ${record.sample_name}`}
function renderDrift(data){state.drift=data;const records=data.records||[],oldStart=$('driftStart').value,oldEnd=$('driftEnd').value;$('driftStart').innerHTML='';$('driftEnd').innerHTML='';records.forEach(record=>[$('driftStart'),$('driftEnd')].forEach(select=>{const option=document.createElement('option');option.value=record.run_id;option.textContent=driftOption(record);select.appendChild(option)}));const saved=data.record_ids||[];if(records.length){$('driftStart').value=oldStart&&records.some(r=>r.run_id===oldStart)?oldStart:(saved[0]||records[0].run_id);$('driftEnd').value=oldEnd&&records.some(r=>r.run_id===oldEnd)?oldEnd:(saved.at(-1)||records.at(-1).run_id)}$('driftSolution').value=data.solution_name||'';$('driftConcentration').value=data.known_concentration_um??'';$('applyDrift').checked=Boolean(data.enabled);$('applyDrift').disabled=data.calculated_at==null;$('calculateDrift').disabled=records.length<2;$('driftStart').disabled=$('driftEnd').disabled=records.length===0;$('driftStartCurrent').textContent=fmt(data.start_current_nA,3);$('driftEndCurrent').textContent=fmt(data.end_current_nA,3);$('driftBias').textContent=data.calculated_at?`${Number(data.bias_nA)>0?'+':''}${fmt(data.bias_nA,3)}`:'--';$('driftSlope').textContent=fmt(data.slope_nA_per_hour,3);$('driftStatus').textContent=records.length<2?`已有 ${records.length} 次稳定化 IT，至少需要 2 次`:data.calculated_at?`${(data.record_ids||[]).length} 次记录 · ${data.enabled?'校正已启用':'校正未启用'}`:`已有 ${records.length} 次稳定化 IT，可选择范围计算`;state.driftDirty=false;}
$('driftSolution').addEventListener('input',()=>{state.driftDirty=true});$('driftConcentration').addEventListener('input',()=>{state.driftDirty=true});$('driftStart').addEventListener('change',()=>{state.driftDirty=true});$('driftEnd').addEventListener('change',()=>{state.driftDirty=true});
$('calculateDrift').onclick=async()=>{try{const data=await post('/api/drift/calculate',{solution_name:$('driftSolution').value,known_concentration_um:$('driftConcentration').value===''?null:$('driftConcentration').value,start_run_id:$('driftStart').value,end_run_id:$('driftEnd').value,enabled:$('applyDrift').checked});renderDrift(data);state.calibration=await api('/api/calibration');renderCalibration();toast('漂移 bias 已计算')}catch(e){toast(e.message)}};
$('applyDrift').addEventListener('change',async()=>{try{renderDrift(await post('/api/drift/toggle',{enabled:$('applyDrift').checked}));state.calibration=await api('/api/calibration');renderCalibration();toast($('applyDrift').checked?'漂移校正已启用':'漂移校正已停用')}catch(e){$('applyDrift').checked=!$('applyDrift').checked;toast(e.message)}});

function renderScheduleMode(){const cv=state.method==='cv',role=$('scheduleRole').value,note={stabilization:'连续运行稳定化 I-T；全部数据自动保存，当前测试曲线保持锁定。',test:'每轮 I-T 完成后使用已锁定曲线自动预测，不更新标定。',calibration:'每轮结果只加入候选标定点；完成后仍需手动选择范围并生成曲线。'};$('scheduleRole').disabled=cv;$('scheduleModeNote').textContent=cv?'按当前 CV 条件完整扫描并逐轮保存；计划间隔必须长于单次扫描时长。':note[role];if(!cv&&role==='stabilization'&&$('schedulePrefix').value==='自动样品')$('schedulePrefix').value='稳定化IT'}
function updateSchedule(data){
  state.schedule=data;
  $('scheduleBadge').textContent=data.active?'运行中':'未运行';
  $('scheduleBadge').className=`live-badge ${data.active?'running':''}`;
  $('scheduleMessage').textContent=data.message;
  $('completedRuns').textContent=data.completed_runs;
  $('startSchedule').disabled=data.active||Boolean(state.measurement?.busy)||!state.settings?.applied;
  $('stopSchedule').disabled=!data.active;
  const next=data.next_run_at?new Date(data.next_run_at*1000):null;
  const stop=data.stop_at?new Date(data.stop_at*1000):null;
  $('nextRun').textContent=next
    ? `下次 ${next.toLocaleTimeString('zh-CN',{hour12:false})}${stop?` · 结束 ${stop.toLocaleTimeString('zh-CN',{hour12:false})}`:''}`
    : '--';
  const list=$('historyList');
  list.replaceChildren();
  if(!data.history?.length){
    const empty=document.createElement('div');
    empty.className='empty-history'; empty.textContent='暂无自动测量记录'; list.appendChild(empty);
    return;
  }
  (data.history||[]).forEach(h=>{
    const el=document.createElement('div'); el.className='history-item';
    const c=h.summary?.steady_current_nA;
    const role={calibration:'候选标定',stabilization:'稳定化',test:'测试',cv:'CV'}[h.metadata?.sample_role]||'';
    const title=document.createElement('strong');
    title.textContent=h.metadata?.sample_name||h.run_id||'';
    const current=document.createElement('span'); current.className='history-current';
    current.textContent=c==null?(h.summary?.cycles_observed?`${h.summary.cycles_observed} 圈`:'--'):`${fmt(c)} nA`;
    const time=document.createElement('small');
    time.textContent=h.finished_at?new Date(h.finished_at*1000).toLocaleString('zh-CN'):'';
    const status=document.createElement('span');
    status.textContent=`${role} · ${h.state==='completed'?'完成':'异常'}`;
    el.append(title,current,time,status); list.appendChild(el);
  });
}
$('scheduleRole').addEventListener('change',renderScheduleMode);
$('startSchedule').onclick=async()=>{try{$('scheduleError').hidden=true;updateSchedule(await post('/api/schedule/start',{interval_minutes:$('intervalMinutes').value,max_runs:$('maxRuns').value,total_minutes:$('totalMinutes').value,sample_prefix:$('schedulePrefix').value,known_concentration_um:$('scheduleConcentration').value===''?null:$('scheduleConcentration').value,sample_role:$('scheduleRole').value,start_now:$('startNow').checked}))}catch(e){errorBox('scheduleError',e)}};
$('stopSchedule').onclick=async()=>{try{updateSchedule(await post('/api/schedule/stop'))}catch(e){errorBox('scheduleError',e)}};

async function init(){setInterval(()=>$('clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false}),1000);await loadFilter();await loadPlateau();try{state.settingsDirty=false;renderSettings(await api('/api/settings'))}catch{}try{renderWorkflow(await api('/api/workflow'))}catch{}try{state.calibration=await api('/api/calibration');renderCalibration()}catch{}try{renderDrift(await api('/api/drift'))}catch{}try{updateSchedule(await api('/api/schedule'))}catch{}try{await refreshWorkspaceHistory()}catch{}try{await refreshDevices(false)}catch{}setSampleRole(state.workflow?.calibration_ready?'test':'calibration',true);previewFilename();measurementRefreshLoop();setInterval(async()=>{if(!state.exiting){try{updateSchedule(await api('/api/schedule'))}catch{}}},1000);try{await post('/api/frontend/ready')}catch{}}

// ══════════════════════════════════════════════════════════════════════════
// 硬件 DEBUG 模式
// ══════════════════════════════════════════════════════════════════════════
// 🔴 与前三页刻意**不共用任何 id 与刷新循环**:这一页 1Hz 拉 /api/debug,
//    前三页 100ms 拉 /api/status。合到一起会让调试页把采集循环的刷新率拖慢。
state.debug = null;
state.dbgStopping = false;
state.dbgProbing = false;
state.dbgChartWindowS = null;   // null = 全程
state.dbgFetchPromise = null;
state.dbgSnapshotGeneration = 0;
function invalidateDebugSnapshots(){
  state.dbgSnapshotGeneration+=1;
  state.dbgFetchPromise=null;
}
async function fetchDebugSnapshot(fresh=false){
  if(fresh)return api('/api/debug');
  if(state.dbgFetchPromise)return state.dbgFetchPromise;
  const request=api('/api/debug');
  state.dbgFetchPromise=request;
  try{return await request}
  finally{if(state.dbgFetchPromise===request)state.dbgFetchPromise=null}
}
function loadDebugLayerPreference(name){
  try{return localStorage.getItem(`sensus.${name}`)!=='false'}catch{return true}
}
function persistDebugLayerPreference(name,value){
  try{localStorage.setItem(`sensus.${name}`,String(Boolean(value)))}catch{}
}
state.dbgShowPotential=loadDebugLayerPreference('debugShowPotential');
state.dbgShowPlateau=loadDebugLayerPreference('debugShowPlateau');
$('dbgShowPotential').checked=state.dbgShowPotential;
$('dbgShowPlateau').checked=state.dbgShowPlateau;

const DBG_FSR_NA = [50, 100, 250, 500, 1000, 2000];
const DBG_OFF_NA = [0, null, null, null, 9, 19, 40, 80];
const DBG_IDLE_NAME = ['停转换(仅对照)', '持续钳位', '断开(CHI 默认)'];

function dbgRow(label, value, hint) {
  return `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(value)}</td>`
    + `<td class="dbg-hint">${escapeHtml(hint || '')}</td></tr>`;
}

// 拒因 → 人能照着做的下一步。🔴 刻意不在 JS 里复算 conv/period 时序:
// 那会变成与 lib/afe_cfg 并列的第二个真值源,而两个真值源必然分叉(本项目已栽过)。
// 这里只把固件给的拒因与数字翻译成动作。
const DBG_REJECT_HINT = {
  period_lt_conv: (r) => `积分时间(${r.a} 个时钟)长于采样周期(${r.b} 个时钟)。`
    + `慢钟组(FSR ≤500 nA)同一个 CONV_TIME 码的积分是快钟组的 4 倍 —— `
    + `所以从 1 µA/2 µA 切到 ≤500 nA 时,原来的 conv 码往往就装不进去了。`
    + `办法:CONV_TIME 选 auto(它会自己挑能装下的最大码),或把 SENS_PERIOD 一起放大。`,
  offset_gt_fsr: () => 'offset 档超过了满量程 ⇒ 还原侧上限无意义、氧化侧为 0。换小 offset 或大 FSR。',
  sysper_short: () => 'SYS_PERIOD 装不下四路电位的总转换时间(约 68 ms)⇒ 会被中途打断。把 SYS_PERIOD 放大。',
  dac: () => '这个 E / V_WE 组合算出的 DAC 电位超出可表达范围(单极性 DAC 取不了负)。调 V_WE 或减小 |E|。',
  dac_mid: () => '改电位的中间态会越过 headroom 限制。分两步走到目标电位。',
  perturb_during_run: () => '这条命令会扰动电解池,而本轮正在采集 ⇒ 默认拒绝。'
    + '确实要改就勾 FORCE(本轮会被标 tainted,数据不得用于标定)。',
  too_long: () => '命令超过 127 字符,整行被丢弃。',
  unknown_key: (r) => `不认识的键 \`${r.key}\`。`,
  dup_key: () => '同一个键写了两次 —— 按笔误处理,整行拒绝。',
  value: () => '值解析不出来。',
  arg: (r) => `参数越界(给的 ${r.a},上限 ${r.b})。`,
  verb: () => '不认识的命令动词。',
  busy: () => '正在采集中 ⇒ OCP 会毁掉本轮,默认拒绝。先「停止」,或带 FORCE。',
  cellv_off: () => 'OCP 要靠电位连采读开路电位,而连采是关的。先 SET cellv=1。',
  idle_keep_biased: () => 'idle=持续钳位 与 OCP(开路)定义冲突。先把 idle 改成 2(断开)。',
};
function renderDbgReject(r) {
  const box = $('dbgReject');
  if (!r) { box.hidden = true; return; }
  // 🔴 从原始整行兜底解析 reason:字段可能缺(旧固件、或别人写的 jsonl)。
  //    缺了就退化成标题重复两遍 kind、且拿不到中文提示 —— 而原始行里明明写着。
  //    原始行是唯一保证存在的东西,所以让它当最后依据。
  const reason = String(r.reason || (/\breason=(\w+)/.exec(r.raw || '') || [])[1] || '');
  const hint = DBG_REJECT_HINT[reason];
  box.hidden = false;
  box.innerHTML = `<b>${escapeHtml(r.kind)}${reason ? ' · ' + escapeHtml(reason) : ' · 未给拒因'}</b>`
    + (hint ? `<br>${hint({...r, reason,
        a: escapeHtml(r.a ?? (/\ba=(-?\d+)/.exec(r.raw || '') || [])[1]),
        b: escapeHtml(r.b ?? (/\bb=(-?\d+)/.exec(r.raw || '') || [])[1]),
        key: escapeHtml(r.key ?? (/\bkey=(\S+)/.exec(r.raw || '') || [])[1])})}` : '')
    + `<br><code style="opacity:.7">${escapeHtml(r.raw || '')}</code>`;
}

function debugPlateauConfig(adaptive){
  adaptive=adaptive||{};
  const runtime=adaptive.config?.settings||adaptive.config||{};
  return {...PLATEAU_DEFAULTS,...(state.plateau?.settings||{}),...runtime};
}
function debugPlateauProgress(progress,config,requiredWindows){
  if(progress===null||progress===undefined||progress==='')return '';
  if(typeof progress==='string')return progress;
  if(Number.isFinite(Number(progress)))return `${fmt(Number(progress)*100,0)}%`;
  if(progress.message)return String(progress.message);
  const elapsed=Number(progress.elapsed_s??progress.covered_s);
  const windowDuration=Number(progress.required_s??progress.window_duration_s??plateauWindowDuration(config));
  const minimumDecision=Number(progress.minimum_decision_s??(
    windowDuration+(Math.max(1,Number(requiredWindows||1))-1)*Number(config?.segment_duration_s||0)
  ));
  if(Number.isFinite(elapsed)&&Number.isFinite(minimumDecision)&&minimumDecision>0){
    const ready=elapsed>=minimumDecision;
    return `${fmt(Math.min(elapsed,minimumDecision),1)} / ${fmt(minimumDecision,1)} s${ready?' · 数据窗已就绪':''}`;
  }
  const segments=Number(progress.complete_segments??progress.segment_count),needed=Number(progress.required_segments);
  if(Number.isFinite(segments)&&Number.isFinite(needed)&&needed>0)return `${segments} / ${needed} 段`;
  return '';
}
function validTailStats(values,valid,limit=20){
  const tail=[];
  for(let index=values.length-1;index>=0&&tail.length<limit;index--){
    const value=Number(values[index]);
    if(valid?.[index]===false||!Number.isFinite(value))continue;
    tail.push(value);
  }
  if(!tail.length)return null;
  const sorted=[...tail].sort((a,b)=>a-b),middle=Math.floor(sorted.length/2);
  const median=sorted.length%2?sorted[middle]:(sorted[middle-1]+sorted[middle])/2;
  const mean=tail.reduce((sum,value)=>sum+value,0)/tail.length;
  const sd=Math.sqrt(tail.reduce((sum,value)=>sum+(value-mean)**2,0)/tail.length);
  return {median,sd,count:tail.length};
}
function renderPlateauDiagnostics(adaptive,running=false){
  adaptive=adaptive||{};
  const evaluation=adaptive.evaluation||null,config=debugPlateauConfig(adaptive);
  const passes=Number(adaptive.consecutive_passes||0),required=Number(adaptive.required_consecutive_windows??config.required_consecutive_windows??2);
  const progress=debugPlateauProgress(adaptive.progress,config,required);
  let status='等待数据';
  if(adaptive.auto_stopped)status='已自动停止';
  else if(evaluation?.stable)status='判定通过';
  else if(evaluation)status=evaluation.reason||'未通过';
  else if(running&&adaptive.monitoring)status='正在累积判定窗';
  else if(adaptive.enabled)status='等待测量';
  $('dbgPlateauStatus').textContent=status;
  $('dbgPlateauStatus').title=status;
  $('dbgPlateauStatus').className=evaluation?.stable||adaptive.auto_stopped?'passed':evaluation?'not-passed':'';
  $('dbgPlateauPasses').textContent=`${passes} / ${required} 连续窗${progress?` · ${progress}`:''}`;
  $('dbgPlateauSlope').textContent=fmt(evaluation?.slope_nA_per_s,4);
  $('dbgPlateauTrend').textContent=fmt(evaluation?.trend_delta_nA??evaluation?.delta_30s_nA,3);
  $('dbgPlateauHalf').textContent=fmt(evaluation?.half_delta_signed_nA??evaluation?.delta_half_nA,3);
  $('dbgPlateauHalfMeans').textContent=`${fmt(evaluation?.first_half_mean_nA,3)} / ${fmt(evaluation?.second_half_mean_nA,3)} nA`;
  $('dbgPlateauScatter').textContent=`${fmt(evaluation?.segment_scatter_nA,3)} / ${fmt(evaluation?.tolerance_nA,3)}`;
  const means=evaluation?.segment_means_nA||[];
  $('dbgPlateauSegments').textContent=means.length?means.map((value,index)=>`S${index+1} ${fmt(value,3)}`).join(' · '):'等待完整判定窗';
}

// 这里只把后端已经计算好的窗口、拟合和均值翻译成图层，不在浏览器
// 内重算 stable/tolerance，避免出现第二套科学判定逻辑。
function debugPlateauLayers(adaptive){
  adaptive=adaptive||{};
  const evaluation=adaptive.evaluation||adaptive.preview,empty={series:[],bands:[]};
  if(!state.dbgShowPlateau||!evaluation)return empty;
  const start=Number(evaluation.window_start_s),end=Number(evaluation.window_end_s);
  if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)return empty;
  const config=debugPlateauConfig(adaptive),means=(evaluation.segment_means_nA||[]).map(Number),centres=(evaluation.segment_centres_s||[]).map(Number);
  const count=Math.min(means.length,centres.length),segmentDuration=Number(config.segment_duration_s)||((end-start)/Math.max(1,count));
  const bands=[],series=[];
  const segments=(evaluation.segments||[]).length?evaluation.segments:(means.slice(0,count).map((mean,index)=>({
    start_s:Math.max(start,centres[index]-segmentDuration/2),end_s:Math.min(end,centres[index]+segmentDuration/2),center_s:centres[index],mean_nA:mean,
  })));
  segments.forEach((segment,index)=>{
    const x0=Number(segment.start_s),x1=Number(segment.end_s),centre=Number(segment.center_s),mean=segment.mean_nA==null?NaN:Number(segment.mean_nA),color=index%2?'#28708c':'#117a65';
    if(!Number.isFinite(x0)||!Number.isFinite(x1)||x1<=x0)return;
    bands.push({x0,x1,color:index%2?'rgba(40,112,140,.065)':'rgba(17,122,101,.065)'});
    if(!Number.isFinite(centre)||!Number.isFinite(mean))return;
    series.push({points:[[x0,mean],[x1,mean]],color,width:2.2});
    series.push({points:[[centre,mean]],color,width:0,dots:true,pointRadius:2.5});
  });
  const backendTrend=(evaluation.trend_line||[]).map(point=>[Number(point.time_s),Number(point.current_nA)]).filter(point=>point.every(Number.isFinite));
  const slope=Number(evaluation.slope_nA_per_s),intercept=Number(evaluation.fit_intercept_nA);
  if(backendTrend.length>=2)series.push({points:backendTrend,color:'#c77a18',width:2,dash:[6,4]});
  else if(Number.isFinite(slope)&&Number.isFinite(intercept))series.push({points:[[start,slope*start+intercept],[end,slope*end+intercept]],color:'#c77a18',width:2,dash:[6,4]});
  const midpoint=(start+end)/2,first=Number(evaluation.first_half_mean_nA),second=Number(evaluation.second_half_mean_nA);
  const halfLines=(evaluation.half_lines||[]).map(line=>({start:Number(line.start_s),end:Number(line.end_s),mean:Number(line.mean_nA)}));
  if(halfLines.length)halfLines.forEach(line=>{if([line.start,line.end,line.mean].every(Number.isFinite))series.push({points:[[line.start,line.mean],[line.end,line.mean]],color:'#52656a',width:1.35,dash:[3,3]})});
  else{
    if(Number.isFinite(first))series.push({points:[[start,first],[midpoint,first]],color:'#52656a',width:1.35,dash:[3,3]});
    if(Number.isFinite(second))series.push({points:[[midpoint,second],[end,second]],color:'#52656a',width:1.35,dash:[3,3]});
  }
  return {series,bands};
}

function renderDebug(d) {
  state.debug = d;
  const cfg = d.cfg || {}, st = d.afe_status || {}, running = d.state === 'running';
  const readOnly = running && !d.debug_run;
  setPlateauFormalRunLock(readOnly);
  const stopping = running && (state.dbgStopping || d.stop_requested);
  const waiting = running && d.waiting_for_start;
  // ── 阶段与静置倒计时 ────────────────────────────────────────────────────
  // 🔴 静置期(Quiet Time)没有电流样本。不显式报阶段的话,界面上"按了没反应"
  //    与"卡死了"完全同形 —— 用户第一反应就是"要等一会儿才有图线"。
  //    固件每秒发一行 IT_PHASE(带 elapsed/total),这里直接渲染,不靠上位机猜配置。
  const ph = d.phase || {};
  const inQuiet = running && ph.phase === 'quiet' && Number(ph.total_ms) > 0;
  const quietLeft = inQuiet
    ? Math.max(0, Math.ceil((Number(ph.total_ms) - Number(ph.elapsed_ms)) / 1000)) : 0;
  const axisNote=state.dbgShowPotential?'左轴电流 nA / 右轴 E (mV)':'电流 nA（电位图层已隐藏）';
  $('dbgBadge').textContent = readOnly ? '正式测量中 · Debug 只读'
    : stopping ? '正在中止…'
    : state.dbgProbing ? '正在读取设备配置…'
    : waiting ? '设备已连接 · 等待开始' : !running
    ? (d.state === 'completed' ? '已完成' : d.state === 'error' ? '错误' : '待机')
    : inQuiet ? `静置 还剩 ${quietLeft} s`
    : ph.phase === 'acquire' ? '采集中'
    : ph.phase === 'idle' ? '本轮已收尾' : '采集中';
  $('dbgBadge').className = `live-badge ${running ? (inQuiet ? 'warn' : 'running') : d.state === 'error' ? 'error' : ''}`;
  $('dbgChartMsg').textContent = inQuiet
    ? `静置中（Quiet Time）：E=${ph.e_mv} mV 已保持 ${Math.round(Number(ph.elapsed_ms) / 1000)}/`
      + `${Math.round(Number(ph.total_ms) / 1000)} s —— 这段刻意不记录电流,`
      + `等双电层充完再开始,否则录到的是瞬态不是稳态`
    : ph.phase === 'acquire'
      ? `采集中：E=${ph.e_mv} mV,目标 ${ph.expected} 个原生点 · ${axisNote}`
      : axisNote;
  $('dbgChartEmpty').textContent = inQuiet
    ? `静置中,还剩 ${quietLeft} s 才开始记录电流（电位曲线已在走）`
    : running ? '等待第一个样本…' : '点「应用并开始一次 I-t」后显示';
  // 🔴 命令只能在测量进行中下发(RTT 下行通道由采集器持有)⇒ 不跑就禁用
  ['dbgGet', 'dbgOcp'].forEach(id => {
    $(id).disabled = !running || stopping || waiting || readOnly;
  });
  $('dbgEpochMsg').textContent = cfg.ep == null ? 'epoch --（尚未收到 CFG_* 行）'
    : `epoch ${cfg.ep}${cfg.confirmed_ep === cfg.ep ? ' · 已确认' : ' · ⚠️ 未确认'}`
      + (cfg.conv_src ? ` · conv=${cfg.conv_src}` : '');
  $('dbgStatusBadge').textContent = st.status1 == null ? '未连接'
    : `STATUS1 0x${Number(st.status1).toString(16).toUpperCase().padStart(2, '0')}`;
  $('dbgStatusBadge').className = `live-badge ${st.invalid_cfg || st.vdd_oor ? 'error' : running ? 'running' : ''}`;

  const c = d.cell_v || {};
  // 🔴 两种灯语义相反,不能共用一个"亮=好"的规则:
  //    故障灯(INVALID_CFG / VDD_OOR / 削顶)= 置位才亮红,未置位保持灰;
  //    就绪灯(PWR_RDY)= 置位亮绿,清零亮红。
  //    把故障灯的"未置位"画成绿色会让人读成"这个故障是 ON 的"—— 与 2026-08-09
  //    那次 sat 阈值不可见导致误报难辨是同一类可读性坑。
  const faultLamp = (id, set) => { $(id).className = `lamp ${set ? 'bad' : ''}`; };
  faultLamp('dbgLampInvalid', Boolean(st.invalid_cfg));
  faultLamp('dbgLampVdd', Boolean(st.vdd_oor));
  // 🔴 四个灯**全是故障灯**:置位=红,未置位=灰。
  //    这一位原来叫 PWR_RDY,而 datasheet p82 说它表示"VDD 曾跌破 1.55V UVLO"
  //    ⇒ 1=掉压(坏)、0=正常,名字与语义相反。固件已改名上报 `brownout=`;
  //    前端一度还在读旧字段 `pwr_rdy` 并按"就绪灯"画 ⇒ 真机上一切正常时亮红。
  faultLamp('dbgLampPwr', Boolean(st.brownout));
  faultLamp('dbgLampClip', Boolean(c.clipped));
  faultLamp('dbgLampRail', Boolean(c.railed));

  // ── 实时电流(用户明确要的:界面上必须有电流值,不能只有图)──────────────
  const cur = d.series?.current || {t: [], nA: [], valid: [], ep: []};
  const filteredCur=filterValues(cur.t||[],cur.nA||[],cur.valid).values;
  const n = filteredCur.length;
  if (n) {
    const last = filteredCur[n - 1], lastOk = cur.valid[n - 1] !== false;
    $('dbgLiveI').textContent = fmt(last, 3);
    $('dbgLiveIAt').textContent = `t = ${fmt(cur.t[n - 1], 2)} s · ep ${cur.ep[n - 1] ?? '?'}`
      + (lastOk ? '' : ' · 🔴 饱和,该点不是测量');
    $('dbgLiveIBox').classList.toggle('invalid', !lastOk);
    const tailStats=validTailStats(filteredCur,cur.valid,20);
    $('dbgIMed').textContent = tailStats?fmt(tailStats.median,3):'--';
    $('dbgISd').textContent = tailStats?fmt(tailStats.sd,3):'--';
    const nsat = cur.valid.filter(v => v === false).length;
    $('dbgN').textContent = String(n);
    $('dbgSat').textContent = nsat ? `🔴 sat ${nsat}` : 'sat 0';
    $('dbgSat').style.color = nsat ? 'var(--red)' : '';
  } else {
    ['dbgLiveI','dbgIMed','dbgISd','dbgN'].forEach(i => { $(i).textContent = '--'; });
    $('dbgLiveIAt').textContent = '尚无数据';
    $('dbgSat').textContent = 'sat 0';
  }
  renderFilterControls(filterValues(cur.t||[],cur.nA||[],cur.valid).meta);
  $('dbgCounts').textContent = cfg.lsb_eff_fa
    ? `${fmt(cfg.lsb_eff_fa / 1000, 3)} pA/码` : '--';
  $('dbgCountsNote').textContent = cfg.bits ? `${cfg.bits} bit 有效台阶` : '原始码';
  // ── 环路饱和的**可行动**诊断 ──────────────────────────────────────────
  // 用户实测:CE 顶在 0 轨、E 死活到不了设定值,手动调 V_WE 又好了。那不是巧合 ——
  // V_WE 只是共模位置(只有 E 有物理意义),抬高它就给 CE 让出了下行空间。
  // 仪器应当自己把这句话说出来,而不是让人从四个电位里去推。
  const setE = Number(cfg.e_mv), gotE = Number(c.e_mv);
  const eErr = (Number.isFinite(setE) && Number.isFinite(gotE)) ? gotE - setE : null;
  const satur = Boolean(c.railed) && eErr !== null && Math.abs(eErr) > 5;
  const warnBox = $('dbgLoopWarn');
  if (satur) {
    const vwe = Number(cfg.vwe_mv) || 1200;
    const need = Math.max(0, Math.round(Number(c.ce_drive_mv || 0)));
    // 上限用**实测 VDD** 算(VDD−1.1V,见 datasheet p11);拿不到才退回旧的 1000 硬顶。
    const ceil = Number.isFinite(Number(c.we_max_mv)) ? Number(c.we_max_mv) : 1000;
    const suggest = Math.min(ceil, Math.round((vwe + Math.abs(eErr) + 300) / 50) * 50);
    warnBox.hidden = false;
    warnBox.innerHTML =
      `<b>恒电位环饱和：E 实测 ${gotE} mV / 设定 ${setE} mV（差 ${eErr.toFixed(0)} mV）</b>`
      + `<br>CE 已顶在 0 轨（它需要比 RE 低 ${need} mV,健康态实测只需约 60 mV）。`
      + `此刻电解池<b>不在设定电位上</b>,这段数据不能用于标定或预测。`
      + `<br>立刻可做：把 <b>V_WE 抬到 ${suggest} mV</b> —— V_WE 只是共模位置,`
      + `只有 E 有物理意义,抬高它就给 CE 让出下行空间。`
      + `<br>但那只是绕过去：根因是 CE 支路需要的驱动变大了（气泡 / 局部干涸 / `
      + `CE 引线接触）。WE 侧可另行核对：WO − WE 正常约 530 mV。`;
  } else { warnBox.hidden = true; }

  renderDbgReject(d.last_reject);

  $('dbgWe').textContent = fmt(c.we_mv, 0); $('dbgRe').textContent = fmt(c.re_mv, 0);
  $('dbgCe').textContent = fmt(c.ce_mv, 0); $('dbgWo').textContent = fmt(c.wo_mv, 0);
  $('dbgCeNote').textContent = c.ce_drive_mv == null ? 'mV'
    : `mV · 驱动 ${fmt(c.ce_drive_mv, 0)} / 到 0.1V 余量 ${fmt(c.ce_headroom_mv, 0)}`;
  // WO 在 V_WE=1200 下必然出量程(WO≈V_WE+540≈1745 > 1.0× 满量程 1536)。
  // 说清是"看不见"而不是"坏了",否则会被当成故障追。
  $('dbgWoNote').textContent = c.wo_offscale
    ? 'mV · ⚠️ 出量程(>1536),V_WE 高时必然如此' : 'mV';
  // VDD 与 V_WE 合法窗口。vdd_mv<=0 ⇒ 固件没报 ⇒ 只说"未上报",不拿假定值冒充。
  const vdd = Number(c.vdd_mv);
  if (Number.isFinite(vdd) && vdd > 0) {
    $('dbgVdd').textContent = fmt(vdd, 0);
    const lo = c.we_min_mv, hi = c.we_max_mv;
    $('dbgVddNote').textContent =
      `mV · V_WE 窗口 ${lo == null ? '?' : fmt(lo, 0)}~${fmt(hi, 0)}`
      + (c.we_headroom_mv == null ? '' : ` · 余量 ${fmt(c.we_headroom_mv, 0)}`);
  } else {
    $('dbgVdd').textContent = '--';
    $('dbgVddNote').textContent = 'mV · 固件未上报(旧版本)';
  }
  $('dbgLiveE').textContent = c.e_mv == null ? '--' : fmt(c.e_mv, 0);
  $('dbgLiveEAt').textContent = c.rows ? `${c.rows} 组 · dev ${fmt(c.dev_ms / 1000, 1)} s` : '尚无数据';
  $('dbgLiveBox').classList.toggle('invalid', Boolean(c.clipped));
  // 🔴 原始 code 必须显示:整池对芯片 GND 浮动时电压会撞 0/4095,只看 mV 看不出削顶
  $('dbgCodes').textContent = c.we_code == null ? '原始 12-bit code:等待数据'
    : `原始 12-bit code — WE ${c.we_code} · RE ${c.re_code} · CE ${c.ce_code} · WO ${c.wo_code}`
      + (c.clipped ? '　⚠️ WE/RE 出界 ⇒ E 不可信' : '')
      + (c.railed ? '　⚠️ CE/WO 撞轨 ⇒ 放大器驱动用尽、环路饱和,电解池未必在设定电位上' : '');

  const rows = [];
  if (cfg.fsr != null) rows.push(dbgRow('FSR', `${DBG_FSR_NA[cfg.fsr]} nA`, `码 ${cfg.fsr} · ${cfg.fsr <= 3 ? '慢钟组' : '快钟组 ×4'}`));
  if (cfg.off_pa != null) rows.push(dbgRow('offset', `${fmt(cfg.off_pa / 1000, 2)} nA`, `档 ${cfg.off} · 容差 ${fmt(cfg.off_min_pa / 1000, 1)}–${fmt(cfg.off_max_pa / 1000, 1)} nA`));
  if (cfg.bits != null) rows.push(dbgRow('分辨率', `${cfg.bits} bit`, `有效 LSB ${fmt(cfg.lsb_eff_fa / 1000, 3)} pA（帧 ${fmt(cfg.lsb_frame_fa / 1000, 3)}）`));
  if (cfg.conv_ms != null) {
    // 积分时间 ≠ 转换时间:差 246 个 precharge 时钟。50Hz 抑制只由**积分时间**决定。
    rows.push(dbgRow('转换 / 周期', `${cfg.conv_ms} / ${cfg.period_ms} ms`, `≈${fmt(1000 / cfg.period_ms, 2)} SPS`));
    rows.push(dbgRow('idle 窗口', `${fmt(cfg.idle_ppm / 10000, 3)} %`, cfg.idle_warn ? '⚠️ >10%,ADC 大量时间在空转' : 'ADC 未在转换的时间占比'));
  }
  if (cfg.rej50_worst_db_x10 != null) rows.push(dbgRow('50 Hz 抑制', `${fmt(cfg.rej50_worst_db_x10 / 10, 1)} dB`, `标称 ${fmt(cfg.rej50_db_x10 / 10, 1)} dB（±2% 时钟下的最坏值才是判据）`));
  if (cfg.conv_alt != null && cfg.conv_alt >= 0) rows.push(dbgRow('conv 次优码', `0x${Number(cfg.conv_alt).toString(16)}`, `${fmt(cfg.conv_alt_db_x10 / 10, 1)} dB`));
  if (cfg.red_max_pa != null) rows.push(dbgRow('可测上限', `还原 ${fmt(cfg.red_max_pa / 1000, 2)} / 氧化 ${fmt(cfg.ox_max_pa / 1000, 1)} nA`, cfg.sig_warn ? '⚠️ offset 盖不住已知信号峰 12.8 nA' : '还原上限 = offset（p41）'));
  if (cfg.sat_margin != null) rows.push(dbgRow('sat 余量', `${cfg.sat_margin} counts`, `= ${fmt(cfg.sat_margin_pa / 1000, 2)} nA`));
  if (cfg.e_mv != null) rows.push(dbgRow('电位', `E ${cfg.e_mv} mV`, `V_WE ${cfg.vwe_mv} · DACA ${cfg.daca} / DACB ${cfg.dacb}${cfg.headroom_warn ? ' ⚠️ EOL 余量不足' : ''}`));
  if (cfg.idle != null) rows.push(dbgRow('idle 处置', DBG_IDLE_NAME[cfg.idle] || cfg.idle, `连采 ${cfg.cellv ? '开' : '关'} · SYS_PERIOD ${cfg.sysper_ms} ms（预算 ${cfg.sysbudget_ms} ms）`));
  if (cfg.ios != null) rows.push(dbgRow('开关位', `chop ${cfg.chop} · rs ${cfg.rs} · ios ${cfg.ios}`, `sel ${cfg.sel} · amps ${cfg.amps} · ioc ${cfg.ioc}`));
  $('dbgParams').querySelector('tbody').innerHTML = rows.join('') ||
    '<tr><td colspan="3" class="dbg-hint">尚未收到 CFG_DERIVED —— 开始一次测量,固件会在开机与每次变更时打出全部派生量</td></tr>';

  $('dbgAuditPath').textContent = d.audit_path || '尚无';
  const events = d.audit || [];
  $('dbgAudit').innerHTML = events.length ? events.map(e => {
    const bad = ['CFG_REJECT', 'CFG_FAULT', 'CFG_ROLLBACK', 'OCP_REJECT'].includes(e.kind);
    const warn = ['IT_TAINTED', 'RANGE_REJECT'].includes(e.kind);
    return `<div class="dbg-event ${bad ? 'bad' : warn ? 'warn' : ''}"><b>${e.kind}</b><code>${
      String(e.raw || '').slice(e.kind.length).trim().replace(/[<>&]/g, ch => ({'<': '&lt;', '>': '&gt;', '&': '&amp;'}[ch]))
    }</code></div>`;
  }).join('') : '<div class="empty-history">开始测量后显示审计事件</div>';

  // 🔴 顺序:先按设备值回填未 dirty 的控件,再渲染按钮与 diff
  dbgSyncFields(cfg);
  renderDbgApply(d);
  renderPlateauDiagnostics(d.adaptive_stop||{},running);
  drawDebug();
}

function drawDebug() {
  const s = state.debug?.series || {};
  const cur = s.current || {t: [], nA: [], valid: []}, cv = s.cell_v || {t: [], e_mv: [], clipped: []};
  const adaptive=state.debug?.adaptive_stop||{},overlay=debugPlateauLayers(adaptive),evaluation=adaptive.evaluation;
  const filteredCur=filterValues(cur.t||[],cur.nA||[],cur.valid), currentValues=filteredCur.values;
  const rawCurPts = [], curPts = [], curBad = [], ePts = [], eBad = [];
  (cur.t || []).forEach((t, i) => {
    const rawPoint=[t,Number(cur.nA[i])];
    if (state.showRaw) rawCurPts.push(rawPoint);
    if (cur.valid?.[i] === false) { if (state.showRaw) curBad.push(rawPoint); }
    else curPts.push([t, currentValues[i]]);
  });
  (cv.t || []).forEach((t, i) => {
    ePts.push([t, cv.e_mv[i]]);
    if (cv.clipped?.[i]) eBad.push([t, cv.e_mv[i]]);
  });
  const latest = Math.max(Number(cur.t?.at(-1))||0,state.dbgShowPotential?(Number(cv.t?.at(-1))||0):0,state.dbgShowPlateau?(Number(evaluation?.window_end_s)||0):0);
  const win = state.dbgChartWindowS;
  let xmin,xmax;
  if(win==='plateau'){
    const start=Number(evaluation?.window_start_s),end=Number(evaluation?.window_end_s),duration=plateauWindowDuration(debugPlateauConfig(adaptive));
    xmin=Number.isFinite(start)?start:Math.max(0,latest-duration);
    xmax=Number.isFinite(end)&&end>xmin?end:Math.max(xmin+duration,latest,1);
  }else{
    xmin=win===null?0:Math.max(0,latest-win);
    xmax=win===null?Math.max(1,latest):Math.max(win,latest);
  }
  const vis = pts => pts.filter(p => p[0] >= xmin && p[0] <= xmax);
  $('dbgChartEmpty').hidden = (cur.t||[]).length>0 || (state.dbgShowPotential&&ePts.length>0);
  drawChart($('dbgChart'), [
    ...(state.showRaw ? [{points: vis(rawCurPts), color: '#b8c0c2', width: .55}] : []),
    {points: vis(curPts), color: '#167b74', width: 1.4},
    ...(state.showRaw ? [{points: vis(curBad), color: '#c33c54', width: 0, dots: true, pointRadius: 1.8}] : []),
    ...(state.dbgShowPotential?[{points:vis(ePts),color:'#8a6fb0',width:1.4,axis:'right'},{points:vis(eBad),color:'#c33c54',width:0,dots:true,pointRadius:2.4,axis:'right'}]:[]),
    ...overlay.series.map(series=>({...series,points:vis(series.points)})),
  ], {xmin, xmax, xlabel: '时间 (s，设备时钟)', ylabel: '电流 (nA)',
      y2label: 'E = V_WE − V_RE (mV)', yDigits: 2, y2Digits: 0,bands:overlay.bands});
  renderFilterControls(filteredCur.meta);
}

[['dbgShowPotential','dbgShowPotential','debugShowPotential'],['dbgShowPlateau','dbgShowPlateau','debugShowPlateau']].forEach(([id,stateKey,storageKey])=>{
  $(id).addEventListener('change',event=>{
    state[stateKey]=event.currentTarget.checked;persistDebugLayerPreference(storageKey,state[stateKey]);
    if(state.debug)renderDebug(state.debug);else drawDebug();
  });
});

// ── 统一参数面板 ───────────────────────────────────────────────────────────
// 每个控件直接显示**设备当前生效值**;改哪个就是哪个。
// 🔴 刻意取消"不改"这种选项:它要求用户在脑内对"设备现在是什么"做减法,
//    而那个信息当时并不在同一个控件里。现在控件本身就是当前值。
const DBG_FIELDS = [
  {id: 'dbgFsr',    key: 'fsr',    from: c => String(c.fsr)},
  {id: 'dbgOff',    key: 'off',    from: c => String(c.off)},
  {id: 'dbgConv',   key: 'conv',   from: c => (c.conv_src === 'auto' ? 'auto' : String(c.conv))},
  {id: 'dbgPeriod', key: 'period', from: c => String(c.period)},
  {id: 'dbgE',      key: 'e',      from: c => String(c.e_mv)},
  {id: 'dbgVwe',    key: 'vwe',    from: c => String(c.vwe_mv)},
  {id: 'dbgIdle',   key: 'idle',   from: c => String(c.idle)},
  {id: 'dbgSysper', key: 'sysper', from: c => String(c.sysper)},
  {id: 'dbgCellv',  key: 'cellv',  from: c => String(c.cellv ? 1 : 0)},
  {id: 'dbgIoc',    key: 'ioc',    from: c => String(c.ioc)},
];
// 用户动过但还没应用的控件。🔴 必须有它:1Hz 刷新会把控件写回设备值,
// 正在选的东西会被抢掉。dirty 的字段不同步,应用成功后清空、让设备值回填 ——
// 于是界面上看到的永远是"设备真的接受了什么",而不是"我打算改什么"。
state.dbgDirty = new Set();
state.dbgCfgSeen = null;

function dbgSyncFields(cfg) {
  if (cfg.fsr === undefined) return;
  // epoch 变了 = 设备已换配置 ⇒ 丢掉全部 dirty,一切以设备为准
  if (state.dbgCfgSeen !== cfg.ep) {
    state.dbgCfgSeen = cfg.ep;
    // 首次探测正是为了补齐未修改字段；用户已修改的字段必须保留到 SET。
    if (!state.dbgProbing) state.dbgDirty.clear();
  }
  DBG_FIELDS.forEach(f => {
    if (state.dbgDirty.has(f.id)) return;
    const want = f.from(cfg);
    if ($(f.id).value !== want) $(f.id).value = want;
  });
}
function dbgFormCfg() {
  const out = {};
  DBG_FIELDS.forEach(f => { out[f.key] = String($(f.id).value).trim(); });
  return out;
}
function dbgDiffKeys(cfg) {
  if (cfg.fsr === undefined) return DBG_FIELDS.map(f => f.key);
  return DBG_FIELDS.filter(f => String($(f.id).value).trim() !== f.from(cfg))
                   .map(f => f.key);
}
// 一条 SET 带**全部**键。未变的键固件侧 plan 会自己 skip(审计行的 skipped= 就是它),
// 所以"全带"既不会多写寄存器,也让这一行成为该 epoch 的完整快照。
function dbgComposeSet(force) {
  const c = dbgFormCfg();
  const parts = DBG_FIELDS.map(f => `${f.key}=${c[f.key]}`);
  return `SET ${parts.join(' ')}${force ? ' FORCE' : ''}`;
}
function dbgValidateForm() {
  const fsr = DBG_FSR_NA[Number($('dbgFsr').value)];
  const offCode = Number($('dbgOff').value);
  const off = offCode === 1 ? fsr * .1
    : offCode === 2 ? fsr * .2
    : offCode === 3 ? fsr * .5 : DBG_OFF_NA[offCode];
  if (Number.isFinite(fsr) && Number.isFinite(off) && off > fsr) {
    throw new Error(`参数组合无效：${fsr} nA 量程不能使用 ${off} nA offset。`
      + '请选择更大的 FSR 或更小的 offset；否则固件会拒绝整组 SET，V_WE 也不会改变。');
  }
}
function renderDbgApply(d) {
  const running = d?.state === 'running';
  const readOnly = running && !d?.debug_run;
  const stopping = running && (state.dbgStopping || d?.stop_requested);
  const waiting = running && d?.waiting_for_start;
  const cfg = d?.cfg || {};
  // 🔴 还没读到设备配置时,控件里是 HTML 默认值而**不是**设备真值。
  //    此时绝不能把表单当成"要应用的配置"下发 —— 那会把猜的值(FSR 50nA、E 空)
  //    写进硬件。改成:这一按只起一轮,让 auto-GET 把真值读回来填表。
  const known = cfg.fsr !== undefined;
  const changed = known ? dbgDiffKeys(cfg) : [];
  const btn = $('dbgApply');
  btn.classList.toggle('warn-btn', running && !readOnly && changed.length > 0);
  btn.textContent = readOnly ? '正式测量中（Debug 只读）'
    : stopping ? '正在中止…'
    : state.dbgProbing ? '正在读取设备配置…'
    : waiting ? '应用参数并开始 I-t'
    : !known ? '开始一次 I-t（先读回设备配置）'
    : running ? (changed.length ? '应用（测量中强制改写）' : '重新下发（测量中）')
    : '应用并开始一次 I-t';
  btn.title = readOnly ? '正式测量期间只允许查看 Debug 诊断，禁止修改硬件或停止测量'
    : stopping ? '等待固件退出采集态并释放 RTT 连接'
    : state.dbgProbing ? '正在通过 RTT 读取设备当前配置，并保留你已修改的字段'
    : waiting ? '先在固件待机态应用参数，再启动采集；本轮不会被标 tainted'
    : !known ? '尚未读到设备配置,本次只起一轮并读回真值,不会写任何参数'
    : running ? '本轮正在采集:改这些参数会扰动电解池,本轮会被标 tainted,数据不得用于标定'
    : '先把参数写进硬件,再立即开始一次 I-t 测量';
  btn.disabled = readOnly || stopping || state.dbgProbing || (waiting && !known);
  $('dbgStop').disabled = !running || stopping || readOnly;
  $('dbgStop').textContent = stopping ? '正在中止…' : '中止';
  DBG_FIELDS.forEach(field => { $(field.id).disabled = readOnly; });
  $('dbgPresetQuiet').disabled = readOnly;
  $('dbgDiff').textContent = readOnly
    ? '当前是正式测量：Debug 页仅显示运行真值和平台诊断，不允许下发命令'
    : !known
    ? '尚未读到设备配置（控件里现在是页面默认值,不是硬件真值）'
    : changed.length ? `将改动 ${changed.length} 项：${changed.join(' · ')}`
    : '与设备当前配置一致';
  $('dbgDiff').classList.toggle('changed', !readOnly && known && changed.length > 0);
}

async function dbgSend(line) {
  try {
    $('dbgError').hidden = true;
    await post('/api/debug/cmd', {line});
    invalidateDebugSnapshots();
    toast(`已下发：${line.length > 60 ? line.slice(0, 60) + '…' : line}`);
    setTimeout(refreshDebug, 500);
  } catch (e) { errorBox('dbgError', e); }
}
async function refreshDebug() {
  try {
    const generation=state.dbgSnapshotGeneration;
    const d = await fetchDebugSnapshot();
    if(generation!==state.dbgSnapshotGeneration)return;
    if (d.state !== 'running') state.dbgStopping = false;
    renderDebug(d);
  }
  catch {
    $('dbgStatusBadge').textContent = '服务未连接';
    $('dbgStatusBadge').className = 'live-badge error';
    $('dbgBadge').textContent = '服务未连接';
    $('dbgBadge').className = 'live-badge error';
    ['dbgGet','dbgOcp','dbgApply','dbgStop'].forEach(id=>{$(id).disabled=true});
  }
}

async function waitForDebugStop(timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const d = await fetchDebugSnapshot(true);
    if (d.state !== 'running') {
      state.dbgStopping = false;
      renderDebug(d);
      return;
    }
    renderDebug(d);
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('中止命令已下发，但采集进程 8 秒内未退出；请查看 collector.log');
}

async function waitForDebugConfig(timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const d = await fetchDebugSnapshot(true);
    state.debug = d;
    if (d.state !== 'running') throw new Error(d.error || '读取设备配置时连接提前退出');
    if (d.cfg?.fsr !== undefined && d.waiting_for_start
        && d.config_session_confirmed) return d;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('8 秒内未读到设备配置，请检查 RTT 连接和 collector.log');
}

async function waitForDebugStart(timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const d = await fetchDebugSnapshot(true);
    state.debug = d;
    renderDebug(d);
    if (d.last_reject) {
      const reason = d.last_reject.reason || 'unknown';
      if (reason === 'offset_gt_fsr') {
        throw new Error('固件拒绝整组参数：offset 大于 FSR。V_WE 未被修改，测量也没有启动。');
      }
      throw new Error(`固件拒绝整组参数（${reason}）。测量没有启动。`);
    }
    if (d.state === 'running' && !d.waiting_for_start && !d.config_pending) return d;
    if (d.state !== 'running') throw new Error(d.error || '等待配置确认时连接提前退出');
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('8 秒内未收到 CFG_CONFIRMED；为避免使用旧参数，测量没有启动');
}

$('dbgChartWindow').addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button) return;
  $('dbgChartWindow').querySelectorAll('button').forEach(x => x.classList.toggle('active', x === button));
  state.dbgChartWindowS = button.dataset.window === 'all' ? null
    : button.dataset.window === 'plateau' ? 'plateau' : Number(button.dataset.window);
  drawDebug();
});
DBG_FIELDS.forEach(f => $(f.id).addEventListener('input', () => {
  state.dbgDirty.add(f.id);
  renderDbgApply(state.debug);
}));
$('dbgGet').addEventListener('click', () => void dbgSend('GET'));
$('dbgOcp').addEventListener('click', () => void dbgSend('OCP 10000'));
// 预设:慢钟 500nA + conv 0x4。SENS_PERIOD 必须一起给到 0x4(1882ms)——
// 1875ms 的积分塞不进 124ms,分两条发必然经过 INVALID_CFG。
// 只填表单、不自动下发:填完让人看一眼 diff 再按「应用」。
$('dbgPresetQuiet').addEventListener('click', () => {
  [['dbgFsr','3'],['dbgConv','4'],['dbgPeriod','4']].forEach(([id, v]) => {
    $(id).value = v; state.dbgDirty.add(id);
  });
  renderDbgApply(state.debug);
  toast('预设已填入表单,确认 diff 后按「应用」');
});

$('dbgApply').addEventListener('click', async () => {
  const running = state.debug?.state === 'running';
  const waiting = running && state.debug?.waiting_for_start;
  const known = state.debug?.cfg?.fsr !== undefined;
  let line = dbgComposeSet(running && !waiting); // 真正采集中才带 FORCE
  let debugSessionStarted = Boolean(waiting && state.debug?.debug_run);
  try {
    $('dbgError').hidden = true;
    dbgValidateForm();
    if (waiting) {
      await post('/api/debug/begin', {line});
      invalidateDebugSnapshots();
      await waitForDebugStart();
      state.dbgDirty.clear();
      toast('参数已在待机态生效，新一轮开始');
      setTimeout(refreshDebug, 600);
      return;
    }
    if (!running) {
      if (!known) {
        // 保存用户已经改过的字段。先建立不启动采集的 ARMED RTT 连接并 GET
        // 设备真值，再让未修改字段回填；最后恢复这些改动，SET → START。
        state.dbgProbing = true;
        renderDbgApply(state.debug);
        await post('/api/debug/start', {note: 'hw-debug-probe', probe_only: true});
        debugSessionStarted = true;
        invalidateDebugSnapshots();
        const probed = await waitForDebugConfig();
        renderDebug(probed);
        line = dbgComposeSet(false);
        await post('/api/debug/begin', {line});
        invalidateDebugSnapshots();
        state.dbgProbing = false;
        await waitForDebugStart();
        state.dbgDirty.clear();
        toast('已读回设备真值，并应用你修改的参数；新一轮开始');
        setTimeout(refreshDebug, 600);
        return;
      }
      // 每一轮都重新读取刚连接后的设备真值；固件确认 SET 后才会发 START。
      await post('/api/debug/start', {note: 'hw-debug', probe_only: true});
      debugSessionStarted = true;
      invalidateDebugSnapshots();
      await waitForDebugConfig();
      await post('/api/debug/begin', {line});
      invalidateDebugSnapshots();
      await waitForDebugStart();
      state.dbgDirty.clear();
      toast('参数已在待机态下发,新一轮开始');
      setTimeout(refreshDebug, 600);
      return;
    }
    await post('/api/debug/cmd', {line});
    invalidateDebugSnapshots();
    state.dbgDirty.clear();
    toast(running ? '已在测量中强制改写参数（本轮已标 tainted）' : '参数已下发,本轮开始');
    setTimeout(refreshDebug, 600);
  } catch (e) {
    state.dbgProbing = false;
    if (debugSessionStarted) {
      try {
        await post('/api/debug/stop');
        invalidateDebugSnapshots();
        await waitForDebugStop();
      } catch {}
    }
    renderDbgApply(state.debug);
    errorBox('dbgError', e);
  }
});
$('dbgStop').addEventListener('click', async () => {
  state.dbgProbing = false;
  state.dbgStopping = true;
  renderDbgApply(state.debug);
  $('dbgError').hidden = true;
  try {
    await post('/api/debug/stop');
    invalidateDebugSnapshots();
    await waitForDebugStop();
    toast('测量已中止，现在可以修改参数');
  } catch (e) {
    state.dbgStopping = false;
    renderDbgApply(state.debug);
    errorBox('dbgError', e);
  }
});
setInterval(refreshDebug,1000);
window.addEventListener('resize',()=>{drawAll();drawDebug()});
init();
