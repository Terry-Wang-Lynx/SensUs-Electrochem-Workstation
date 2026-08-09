const $ = (id) => document.getElementById(id);
const state = { measurement: null, calibration: {points: [], model: null, curve: null}, drift: null, schedule: null, settings: null, workflow: null, sampleRole: 'calibration', chartWindowS: 5, lastHandledRunId: null };
const pages = {
  measure: ['实时测量', '180 秒 IT 检测与末 20 秒稳态分析'],
  calibrate: ['标定与漂移', '选择标定范围并管理过渡期 bias'],
  schedule: ['稳定化 / 自动', '无人值守的定时连续 IT 检测']
};

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}
function post(path, body = {}) { return api(path, {method: 'POST', body: JSON.stringify(body)}); }
function toast(message) { const node = $('toast'); node.textContent = message; node.classList.add('show'); setTimeout(() => node.classList.remove('show'), 2600); }
function errorBox(id, error) { const node = $(id); node.textContent = error?.message || String(error); node.hidden = false; }
function fmt(value, digits = 2) { return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '--'; }
function concentrationValue(){return $('knownConcentration').value===''?null:$('knownConcentration').value}
function previewFilename(){const sample=($('sampleName').value||'样品名称').trim().replace(/[\\/:*?"<>|]/g,'_'), concentration=concentrationValue();$('autoSaveName').textContent=`${sample}-${concentration===null?'unknown':`${Number(concentration)}uM`}.csv`}
function updateStartState(){
  const running=state.measurement?.state==='running', ready=state.workflow?.calibration_ready;
  $('startMeasure').disabled=running||!state.settings?.applied||!state.workflow?.save_dir||(state.sampleRole==='test'&&!ready);
  $('startMeasure').textContent=state.sampleRole==='calibration'?'开始标定测量':'开始测试并预测';
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
  const labels={collect:'采集中',select:'待选范围',stabilization:'稳定化中',test:'曲线已锁定'};
  $('workflowBadge').textContent=labels[data.stage]||'待配置';$('workflowBadge').className=`live-badge ${data.calibration_ready?'running':''}`;
  $('workflowMessage').textContent=data.calibration_ready?`测试曲线采用 ${data.selected_points_count} / ${data.points_count} 个候选点；后续采集不会自动改写`:data.points_count&&!data.settings_match?'当前 IT 条件与已有标定不同；请恢复原条件或新建标定批次':`已记录 ${data.points_count} 个候选点，请到“标定与漂移”选择用于拟合的范围`;
  $('calibrationStep').classList.toggle('active',data.stage==='collect');$('selectionStep').classList.toggle('active',data.stage==='select');$('testStep').classList.toggle('active',['stabilization','test'].includes(data.stage));
  const testButton=$('sampleRole').querySelector('[data-role="test"]');testButton.disabled=!data.calibration_ready;
  ['test','stabilization'].forEach(role=>$('scheduleRole').querySelector(`option[value="${role}"]`).disabled=!data.calibration_ready);
  if(!data.calibration_ready&&['test','stabilization'].includes($('scheduleRole').value))$('scheduleRole').value='calibration';
  if(!data.calibration_ready&&state.sampleRole==='test')setSampleRole('calibration',true);
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
  if(result.sample_role==='calibration'){
    toast('候选标定点已保存；当前测试曲线未被改写');
  }else if(result.sample_role==='stabilization'){
    toast('稳定化 IT 已保存；测试曲线保持锁定');
  }else{
    $('predictionResult').hidden=false;$('predictionResult').querySelector('strong').textContent=fmt(result.predicted_concentration_um,3);
    toast('测试完成，浓度已自动预测并保存');
  }
}

document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x === button));
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  $(`view-${button.dataset.view}`).classList.add('active');
  $('pageTitle').textContent = pages[button.dataset.view][0];
  $('pageSubtitle').textContent = pages[button.dataset.view][1];
  requestAnimationFrame(drawAll);
}));

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1, rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, rect.width * ratio); canvas.height = Math.max(1, rect.height * ratio);
  const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); return {ctx, w: rect.width, h: rect.height};
}
function drawChart(canvas, series, options = {}) {
  const {ctx, w, h} = setupCanvas(canvas); ctx.clearRect(0, 0, w, h);
  const all = series.flatMap(s => s.points).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1])); if (!all.length) return;
  let xmin = options.xmin ?? Math.min(...all.map(p => p[0])), xmax = options.xmax ?? Math.max(...all.map(p => p[0]));
  let ymin = Math.min(...all.map(p => p[1])), ymax = Math.max(...all.map(p => p[1]));
  if (xmax === xmin) xmax = xmin + 1; if (ymax === ymin) {ymin -= 1; ymax += 1;} const padY = (ymax-ymin)*.12; ymin -= padY; ymax += padY;
  const m = {l:56,r:18,t:18,b:38}, px = x => m.l+(x-xmin)/(xmax-xmin)*(w-m.l-m.r), py = y => m.t+(ymax-y)/(ymax-ymin)*(h-m.t-m.b);
  ctx.font = '10px system-ui'; ctx.fillStyle='#718086'; ctx.strokeStyle='#e2e7e8'; ctx.lineWidth=1;
  for(let i=0;i<=5;i++){const y=ymin+(ymax-ymin)*i/5, yy=py(y);ctx.beginPath();ctx.moveTo(m.l,yy);ctx.lineTo(w-m.r,yy);ctx.stroke();ctx.fillText(y.toFixed(1),6,yy+3)}
  for(let i=0;i<=6;i++){const x=xmin+(xmax-xmin)*i/6, xx=px(x);ctx.beginPath();ctx.moveTo(xx,m.t);ctx.lineTo(xx,h-m.b);ctx.stroke();ctx.fillText(x.toFixed(xmax<=60?1:0),xx-8,h-15)}
  series.forEach(s => {
    const points = s.points.filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
    const width = s.width ?? 1.6;
    if (width > 0 && points.length) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = width;
      ctx.beginPath();
      points.forEach((p, i) => i ? ctx.lineTo(px(p[0]), py(p[1])) : ctx.moveTo(px(p[0]), py(p[1])));
      ctx.stroke();
    }
    if (s.dots) {
      ctx.fillStyle = s.color;
      const radius = s.pointRadius ?? 4;
      points.forEach(p => {
        ctx.beginPath();
        ctx.arc(px(p[0]), py(p[1]), radius, 0, Math.PI * 2);
        ctx.fill();
      });
    }
  });
  ctx.fillStyle='#68767b';ctx.fillText(options.xlabel||'Time (s)',w/2-22,h-2);ctx.save();ctx.translate(12,h/2+25);ctx.rotate(-Math.PI/2);ctx.fillText(options.ylabel||'Current (nA)',0,0);ctx.restore();
}
function drawAll(){
  const d = state.measurement?.data || {};
  const rawPoints = [], validPoints = [], invalidPoints = [];
  (d.time_s || []).forEach((time, i) => {
    const point = [time, d.current_nA[i]];
    rawPoints.push(point);
    (d.valid?.[i] === false ? invalidPoints : validPoints).push(point);
  });
  const duration = Number(state.measurement?.settings?.duration_s || state.settings?.settings?.duration_s || 180);
  const latest = rawPoints.at(-1)?.[0] || 0;
  const xmin = state.chartWindowS === null ? 0 : Math.max(0, latest - state.chartWindowS);
  const xmax = state.chartWindowS === null ? duration : Math.max(state.chartWindowS, latest);
  const visible = points => points.filter(point => point[0] >= xmin && point[0] <= xmax);
  const itSeries = [
    {points: visible(rawPoints), color: '#9aa7aa', width: 0.7},
    {points: visible(validPoints), color: '#167b74', width: 0, dots: true, pointRadius: 1.5},
    {points: visible(invalidPoints), color: '#c33c54', width: 0, dots: true, pointRadius: 1.7},
  ];
  $('chartEmpty').hidden = rawPoints.length > 0;
  drawChart($('itChart'), itSeries, {xmin, xmax, xlabel: '时间 (s)', ylabel: '电流 (nA)'});
  const c=state.calibration, series=[];
  if(c.curve)series.push({points:c.curve.concentration_um.map((x,i)=>[x,c.curve.current_nA[i]]),color:'#c77a18',width:2});
  const selected=(c.points||[]).filter(p=>p.selected);
  if(selected.length)series.push({points:selected.map(p=>[Number(p.concentration_um),Number(p.current_nA)]),color:'#28708c',dots:true,width:0,pointRadius:4});
  $('calibrationEmpty').hidden=series.length>0;drawChart($('calibrationChart'),series,{xlabel:'浓度 (µM)',ylabel:'电流 (nA)'});
}

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

function updateMeasurement(data){
  state.measurement=data; const running=data.state==='running', complete=data.state==='completed';
  renderRange(data); renderTransient(data);
  $('measureMessage').textContent=data.message||''; $('liveBadge').textContent=running?'采集中':complete?'已完成':data.state==='error'?'错误':'待机'; $('liveBadge').className=`live-badge ${running?'running':data.state==='error'?'error':''}`;
  $('stopMeasure').disabled=!running; $('useForCalibration').disabled=!complete||data.summary?.steady_current_nA==null; $('predictConcentration').disabled=!complete||data.summary?.steady_current_nA==null;
  const s=data.summary||{}; $('steadyCurrent').textContent=fmt(s.steady_current_nA); $('steadySd').textContent=fmt(s.steady_sd_nA);
  const live=data.latest_sample;
  $('liveCurrent').textContent=live?fmt(live.current_nA,3):'--';
  $('liveCurrentTime').textContent=live?`t = ${fmt(live.time_s,2)} s · 点 ${Number(live.index)+1}`:'尚无数据';
  $('liveCurrentBox').classList.toggle('invalid',Boolean(live&&!live.valid));
  const latest=data.data?.time_s?.at(-1)||0, duration=Number(data.settings?.duration_s||180), nativeCount=data.data?.time_s?.length||0;
  const nativeRate=1000/[124,242,476,945,1882,3757][Number(data.settings?.sens_period_code||0)], expectedNative=Math.round(duration*nativeRate);
  $('validCount').textContent=nativeCount||'--'; $('validCount').nextElementSibling.textContent=`/ ≈ ${expectedNative}`;
  $('progressValue').textContent=complete?100:Math.min(100,Math.round(latest/duration*100));
  const hardwareState=data.state==='error'?'error':running?'busy':data.state==='completed'?'ok':'';
  $('deviceDot').className=`status-dot ${hardwareState}`;
  $('deviceState').textContent=data.state==='error'?'硬件 / 采集异常':running?'设备测量中':data.state==='completed'?'上轮测量完成':'硬件待测';
  if(data.error){$('measureError').textContent=data.error;$('measureError').hidden=false}else $('measureError').hidden=true; updateStartState();drawAll();void handleWorkflowCompletion(data);
}
async function refreshMeasurement(){try{updateMeasurement(await api('/api/status'))}catch(e){$('deviceState').textContent='服务未连接';$('deviceDot').className='status-dot'}}
async function measurementRefreshLoop(){
  await refreshMeasurement();
  const running = state.measurement?.state === 'running';
  const duration = Number(state.measurement?.settings?.duration_s || 180);
  setTimeout(measurementRefreshLoop, running ? (duration <= 300 ? 100 : 500) : 1000);
}
$('chartWindow').addEventListener('click', event => {
  const button = event.target.closest('button[data-window]');
  if (!button) return;
  state.chartWindowS = button.dataset.window === 'all' ? null : Number(button.dataset.window);
  $('chartWindow').querySelectorAll('button').forEach(node => node.classList.toggle('active', node === button));
  drawAll();
});
$('startMeasure').addEventListener('click',async()=>{try{$('measureError').hidden=true;updateMeasurement(await post('/api/measurement/start',{sample_name:$('sampleName').value,known_concentration_um:concentrationValue(),sample_role:state.sampleRole,save_dir:$('saveDirectory').value,source:'manual_gui'}))}catch(e){errorBox('measureError',e)}});
$('stopMeasure').addEventListener('click',async()=>{try{updateMeasurement(await post('/api/measurement/stop'))}catch(e){errorBox('measureError',e)}});
$('sampleRole').addEventListener('click',event=>{const button=event.target.closest('button[data-role]');if(button)setSampleRole(button.dataset.role)});
$('sampleName').addEventListener('input',previewFilename);$('knownConcentration').addEventListener('input',previewFilename);
$('applySaveDirectory').onclick=async()=>{try{renderWorkflow(await post('/api/workflow/config',{save_dir:$('saveDirectory').value}));state.calibration=await api('/api/calibration');renderCalibration();toast('保存目录已应用')}catch(e){errorBox('measureError',e)}};
$('resetCalibration').onclick=async()=>{if(!confirm('开始一套新标定？现有标定文件会保留为带时间戳的归档。'))return;try{renderWorkflow(await post('/api/workflow/reset-calibration'));state.calibration=await api('/api/calibration');renderCalibration();setSampleRole('calibration',true);toast('已开始新的标定')}catch(e){errorBox('measureError',e)}};

function readSettings(){const potential=Number($('potentialV').value);return {initial_potential_v:potential,potential_v:potential,prestep_s:0,duration_s:Number($('durationS').value),sens_period_code:Number($('sensPeriodCode').value),target_rate_hz:Number($('sampleRateHz').value),fit_window_s:Number($('fitWindowS').value),fsr_nA:Number($('fsrNA').value),offset_mode:$('offsetNA').value}}
function renderOffsetLabels(fsr){$('offsetNA').querySelectorAll('option[data-pct]').forEach(option=>{const pct=Number(option.dataset.pct);option.textContent=`${pct}% FSR (${fsr*pct/100} nA)`})}
function signedPotential(value){const number=Number(value);return `${number>0?'+':''}${number.toFixed(2)}`}
function renderSettings(data){
  state.settings=data; const s=data.settings, periods=[124,242,476,945,1882,3757], nativeRate=1000/periods[s.sens_period_code??0];
  $('potentialV').value=s.potential_v; $('durationS').value=s.duration_s; $('sensPeriodCode').value=String(s.sens_period_code??0); $('sampleRateHz').value=s.target_rate_hz; $('fitWindowS').value=s.fit_window_s; $('fsrNA').value=String(s.fsr_nA);renderOffsetLabels(s.fsr_nA);$('offsetNA').value=s.offset_mode||`${s.offset_nA??19}nA`;
  $('outputPoints').textContent=`${Math.round(s.duration_s*s.target_rate_hz)} 点 · 原生 ${fmt(nativeRate,2)} Hz`;
  pages.measure[1]=`${s.duration_s} 秒 IT 检测与末 ${s.fit_window_s} 秒稳态分析`; if($('view-measure').classList.contains('active'))$('pageSubtitle').textContent=pages.measure[1]; $('validCount').nextElementSibling.textContent=`/ ${Math.round(s.duration_s*s.target_rate_hz)}`;
  // state==='error' 时把真正的 data.error 顶到消息栏,而不是只显示笼统的 data.message。
  // 起因:2026-08-09 烧录失败(`command not found: west`)整个过程 <1s,label 闪一下就
  // 弹回,真实原因只进了右下角那个小 #measureError 框 ⇒ 现象看起来是「点了没反应」。
  const settingsFailed=data.state==='error', failDetail=String(data.error||'').trim();
  $('settingsMessage').textContent=settingsFailed&&failDetail?failDetail:data.message; $('settingsMessage').title=settingsFailed&&failDetail?failDetail:''; $('settingsMessage').classList.toggle('error-text',settingsFailed&&!!failDetail);
  $('settingsBadge').textContent=data.state==='applying'?'应用中':settingsFailed?'失败':data.applied?'已应用':'未应用'; $('settingsBadge').className=`live-badge ${settingsFailed?'error':data.applied?'running':''}`;
  // 🔴 error 必须排在 applied 前面:apply() 失败时服务端**不会**把 applied 复位成 false
  //    (板上仍是上一次的固件,所以它保持 true)。原来的 `applied?'running':...` 顺序
  //    会让徽章文案写「失败」却配绿色 running 样式 —— 2026-08-09 用浏览器实测到。
  $('firmwareNote').textContent=`${signedPotential(s.potential_v)} V 恒电位 · ${s.fsr_nA} nA FSR`; $('scheduleMethodLabel').textContent=`恒电位 IT · ${s.duration_s} 秒 · ${signedPotential(s.potential_v)} V`;
  const minInterval=(s.duration_s+10)/60; $('intervalMinutes').min=minInterval.toFixed(2); if(Number($('intervalMinutes').value)<minInterval)$('intervalMinutes').value=Math.ceil(minInterval*4)/4;
  updateStartState(); $('startSchedule').disabled=state.schedule?.active||!data.applied;
}
function settingsChanged(){if(!state.settings)return;state.settings.applied=false;state.settings.state='not_applied';state.settings.message='参数已修改，请重新应用';const s=readSettings();state.settings.settings=s;renderSettings(state.settings)}
['potentialV','durationS','sensPeriodCode','sampleRateHz','fitWindowS','fsrNA','offsetNA'].forEach(id=>$(id).addEventListener('change',settingsChanged));
$('applySettings').onclick=async()=>{try{$('applySettings').disabled=true;$('applySettings').textContent='正在编译并烧录…';renderSettings({...state.settings,settings:readSettings(),state:'applying',message:'正在编译并写入硬件参数',applied:false});const data=await post('/api/settings/apply',readSettings());renderSettings(data);renderWorkflow(await api('/api/workflow'));toast('IT 条件已应用到硬件')}catch(e){errorBox('measureError',e);try{renderSettings(await api('/api/settings'))}catch{}}finally{$('applySettings').disabled=false;$('applySettings').textContent='应用条件并烧录硬件'}};

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
function syncCalibrationPreview(){updateSelectedCount();state.calibration.points=readPoints();drawAll()}
$('addPoint').onclick=()=>{$('pointsBody').appendChild(row({},$('pointsBody').children.length));refreshRangeControls();syncCalibrationPreview()};
$('applyPointRange').onclick=()=>{const rows=[...$('pointsBody').querySelectorAll('tr')],a=Math.min(Number($('rangeStart').value),Number($('rangeEnd').value)),b=Math.max(Number($('rangeStart').value),Number($('rangeEnd').value));rows.forEach((tr,index)=>tr.querySelector('.point-selector').checked=index>=a&&index<=b);syncCalibrationPreview()};
$('clearPointSelection').onclick=()=>{$('pointsBody').querySelectorAll('.point-selector').forEach(input=>input.checked=false);syncCalibrationPreview()};
$('useForCalibration').onclick=()=>{const current=state.measurement?.summary?.steady_current_nA, concentration=$('knownConcentration').value;if(concentration===''){toast('请先填写已知浓度');return}$('pointsBody').appendChild(row({label:$('sampleName').value||state.measurement.run_id,concentration_um:concentration,current_nA:current}));document.querySelector('[data-view="calibrate"]').click();toast('已加入标定数据')};
$('predictConcentration').onclick=async()=>{try{const result=await post('/api/predict',{});$('predictionResult').querySelector('strong').textContent=fmt(result.predicted_concentration_um,3);toast('浓度预测完成')}catch(e){errorBox('measureError',e)}};
$('fitCalibration').onclick=async()=>{try{const points=readPoints(),selected=points.filter(point=>point.selected).map(point=>point.point_id);if(!selected.length){toast('请先选择一个标定点范围');return}const data=await post('/api/calibration/fit',{points,selected_point_ids:selected,degree:Number($('fitDegree').value)});state.calibration=data;renderCalibration();renderWorkflow(await api('/api/workflow'));setSampleRole('test',true);toast('选中范围已生成并锁定为测试曲线')}catch(e){toast(e.message)}};
function renderCalibration(){const {model,points,model_path,model_created_at,drift_bias_nA,model_compatible}=state.calibration;if(points)renderPoints(points);$('modelR2').textContent=model?fmt(model.r2,4):'--';$('modelRmse').textContent=model?`${fmt(model.rmse_nA,2)} nA`:'--';$('modelSlope').textContent=model&&model.degree===1?`${fmt(model.coefficients[0],3)} nA/µM`:'--';const bias=Number(drift_bias_nA||0);$('calibrationStatus').textContent=model&&!model_compatible?'旧条件曲线 · 当前 IT 条件不匹配，测试已禁用':model?`已锁定 ${model.n_points} 个选中点 · ${model.concentration_min_um}–${model.concentration_max_um} µM${bias?` · bias ${bias>0?'+':''}${fmt(bias,3)} nA`:''}`:'选择至少两个不同浓度的候选点';$('modelPath').textContent=model_path?`${model_path}${model_created_at?` · ${new Date(model_created_at*1000).toLocaleString('zh-CN',{hour12:false})}`:''}`:'尚未生成测试曲线';drawAll()}

function driftOption(record){const date=new Date(record.finished_at*1000).toLocaleString('zh-CN',{hour12:false});return `${date} · ${fmt(record.steady_current_nA,3)} nA · ${record.sample_name}`}
function renderDrift(data){state.drift=data;const records=data.records||[],oldStart=$('driftStart').value,oldEnd=$('driftEnd').value;$('driftStart').innerHTML='';$('driftEnd').innerHTML='';records.forEach(record=>[$('driftStart'),$('driftEnd')].forEach(select=>{const option=document.createElement('option');option.value=record.run_id;option.textContent=driftOption(record);select.appendChild(option)}));const saved=data.record_ids||[];if(records.length){$('driftStart').value=oldStart&&records.some(r=>r.run_id===oldStart)?oldStart:(saved[0]||records[0].run_id);$('driftEnd').value=oldEnd&&records.some(r=>r.run_id===oldEnd)?oldEnd:(saved.at(-1)||records.at(-1).run_id)}$('driftSolution').value=data.solution_name||'';$('driftConcentration').value=data.known_concentration_um??'';$('applyDrift').checked=Boolean(data.enabled);$('applyDrift').disabled=data.calculated_at==null;$('calculateDrift').disabled=records.length<2;$('driftStart').disabled=$('driftEnd').disabled=records.length===0;$('driftStartCurrent').textContent=fmt(data.start_current_nA,3);$('driftEndCurrent').textContent=fmt(data.end_current_nA,3);$('driftBias').textContent=data.calculated_at?`${Number(data.bias_nA)>0?'+':''}${fmt(data.bias_nA,3)}`:'--';$('driftSlope').textContent=fmt(data.slope_nA_per_hour,3);$('driftStatus').textContent=records.length<2?`已有 ${records.length} 次稳定化 IT，至少需要 2 次`:data.calculated_at?`${(data.record_ids||[]).length} 次记录 · ${data.enabled?'校正已启用':'校正未启用'}`:`已有 ${records.length} 次稳定化 IT，可选择范围计算`;}
$('calculateDrift').onclick=async()=>{try{const data=await post('/api/drift/calculate',{solution_name:$('driftSolution').value,known_concentration_um:$('driftConcentration').value===''?null:$('driftConcentration').value,start_run_id:$('driftStart').value,end_run_id:$('driftEnd').value,enabled:$('applyDrift').checked});renderDrift(data);state.calibration=await api('/api/calibration');renderCalibration();toast('漂移 bias 已计算')}catch(e){toast(e.message)}};
$('applyDrift').addEventListener('change',async()=>{try{renderDrift(await post('/api/drift/toggle',{enabled:$('applyDrift').checked}));state.calibration=await api('/api/calibration');renderCalibration();toast($('applyDrift').checked?'漂移校正已启用':'漂移校正已停用')}catch(e){$('applyDrift').checked=!$('applyDrift').checked;toast(e.message)}});

function renderScheduleMode(){const role=$('scheduleRole').value,note={stabilization:'连续运行稳定化 IT；全部数据自动保存，当前测试曲线保持锁定。',test:'每轮 IT 完成后使用已锁定曲线自动预测，不更新标定。',calibration:'每轮结果只加入候选标定点；完成后仍需手动选择范围并生成曲线。'};$('scheduleModeNote').textContent=note[role];if(role==='stabilization'&&$('schedulePrefix').value==='自动样品')$('schedulePrefix').value='稳定化IT'}
function updateSchedule(data){state.schedule=data;$('scheduleBadge').textContent=data.active?'运行中':'未运行';$('scheduleBadge').className=`live-badge ${data.active?'running':''}`;$('scheduleMessage').textContent=data.message;$('completedRuns').textContent=data.completed_runs;$('startSchedule').disabled=data.active||!state.settings?.applied;$('stopSchedule').disabled=!data.active;const next=data.next_run_at?new Date(data.next_run_at*1000):null,stop=data.stop_at?new Date(data.stop_at*1000):null;$('nextRun').textContent=next?`下次 ${next.toLocaleTimeString('zh-CN',{hour12:false})}${stop?` · 结束 ${stop.toLocaleTimeString('zh-CN',{hour12:false})}`:''}`:'--';const list=$('historyList');list.innerHTML=data.history?.length?'':'<div class="empty-history">暂无自动测量记录</div>';(data.history||[]).forEach(h=>{const el=document.createElement('div');el.className='history-item';const c=h.summary?.steady_current_nA,role={calibration:'候选标定',stabilization:'稳定化',test:'测试'}[h.metadata?.sample_role]||'';el.innerHTML=`<strong>${h.metadata?.sample_name||h.run_id}</strong><span class="history-current">${fmt(c)} nA</span><small>${h.finished_at?new Date(h.finished_at*1000).toLocaleString('zh-CN'):''}</small><span>${role} · ${h.state==='completed'?'完成':'异常'}</span>`;list.appendChild(el)})}
$('scheduleRole').addEventListener('change',renderScheduleMode);
$('startSchedule').onclick=async()=>{try{$('scheduleError').hidden=true;updateSchedule(await post('/api/schedule/start',{interval_minutes:$('intervalMinutes').value,max_runs:$('maxRuns').value,total_minutes:$('totalMinutes').value,sample_prefix:$('schedulePrefix').value,known_concentration_um:$('scheduleConcentration').value===''?null:$('scheduleConcentration').value,sample_role:$('scheduleRole').value,start_now:$('startNow').checked}))}catch(e){errorBox('scheduleError',e)}};
$('stopSchedule').onclick=async()=>{try{updateSchedule(await post('/api/schedule/stop'))}catch(e){errorBox('scheduleError',e)}};

async function init(){setInterval(()=>$('clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false}),1000);try{renderSettings(await api('/api/settings'))}catch{}try{renderWorkflow(await api('/api/workflow'))}catch{}try{state.calibration=await api('/api/calibration');renderCalibration()}catch{}try{renderDrift(await api('/api/drift'))}catch{}try{updateSchedule(await api('/api/schedule'))}catch{}setSampleRole(state.workflow?.calibration_ready?'test':'calibration',true);previewFilename();measurementRefreshLoop();setInterval(async()=>{try{updateSchedule(await api('/api/schedule'))}catch{}},1000)}
window.addEventListener('resize',drawAll);init();
