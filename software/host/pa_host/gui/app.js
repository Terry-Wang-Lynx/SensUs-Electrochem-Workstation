const $ = (id) => document.getElementById(id);
const state = { measurement: null, calibration: {points: [], model: null, curve: null}, drift: null, schedule: null, settings: null, workflow: null, sampleRole: 'calibration', chartWindowS: 5, lastHandledRunId: null };
const pages = {
  measure: ['实时测量', '180 秒 IT 检测与末 20 秒稳态分析'],
  calibrate: ['标定与漂移', '选择标定范围并管理过渡期 bias'],
  schedule: ['稳定化 / 自动', '无人值守的定时连续 IT 检测'],
  // 🔴 缺这一条会让整个切页 handler TypeError —— pages[view][0] 直接取下标
  debug: ['硬件 DEBUG', '运行时改采样参数 · 每次变更留审计 · 电流与电位同图']
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
  requestAnimationFrame(() => {drawAll(); drawDebug();});
}));

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
  let [ymin, ymax] = span(allL.length ? allL : all);
  const [y2min, y2max] = span(allR);
  if (xmax === xmin) xmax = xmin + 1;
  const m = {l:56, r: hasRight ? 56 : 18, t:18, b:38};
  const px = x => m.l+(x-xmin)/(xmax-xmin)*(w-m.l-m.r);
  const py = y => m.t+(ymax-y)/(ymax-ymin)*(h-m.t-m.b);
  const py2 = y => m.t+(y2max-y)/(y2max-y2min)*(h-m.t-m.b);
  const yd = options.yDigits ?? 1, y2d = options.y2Digits ?? 0;
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

function updateMeasurement(data){
  state.measurement=data; const running=data.state==='running', complete=data.state==='completed';
  renderRange(data); renderTransient(data); renderCellV(data);
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

// ══════════════════════════════════════════════════════════════════════════
// 硬件 DEBUG 模式
// ══════════════════════════════════════════════════════════════════════════
// 🔴 与前三页刻意**不共用任何 id 与刷新循环**:这一页 1Hz 拉 /api/debug,
//    前三页 100ms 拉 /api/status。合到一起会让调试页把采集循环的刷新率拖慢。
state.debug = null;
state.dbgChartWindowS = null;   // null = 全程

const DBG_FSR_NA = [50, 100, 250, 500, 1000, 2000];
const DBG_IDLE_NAME = ['停转换(仅对照)', '持续钳位', '断开(CHI 默认)'];

function dbgRow(label, value, hint) {
  return `<tr><th>${label}</th><td>${value}</td><td class="dbg-hint">${hint || ''}</td></tr>`;
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
  box.innerHTML = `<b>${r.kind}${reason ? ' · ' + reason : ' · 未给拒因'}</b>`
    + (hint ? `<br>${hint({...r, reason,
        a: r.a ?? (/\ba=(-?\d+)/.exec(r.raw || '') || [])[1],
        b: r.b ?? (/\bb=(-?\d+)/.exec(r.raw || '') || [])[1],
        key: r.key ?? (/\bkey=(\S+)/.exec(r.raw || '') || [])[1]})}` : '')
    + `<br><code style="opacity:.7">${String(r.raw || '').replace(/[<>&]/g, ch => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[ch]))}</code>`;
}

function renderDebug(d) {
  state.debug = d;
  const cfg = d.cfg || {}, st = d.afe_status || {}, running = d.state === 'running';
  // ── 阶段与静置倒计时 ────────────────────────────────────────────────────
  // 🔴 静置期(Quiet Time)没有电流样本。不显式报阶段的话,界面上"按了没反应"
  //    与"卡死了"完全同形 —— 用户第一反应就是"要等一会儿才有图线"。
  //    固件每秒发一行 IT_PHASE(带 elapsed/total),这里直接渲染,不靠上位机猜配置。
  const ph = d.phase || {};
  const inQuiet = running && ph.phase === 'quiet' && Number(ph.total_ms) > 0;
  const quietLeft = inQuiet
    ? Math.max(0, Math.ceil((Number(ph.total_ms) - Number(ph.elapsed_ms)) / 1000)) : 0;
  $('dbgBadge').textContent = !running
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
      ? `采集中：E=${ph.e_mv} mV,目标 ${ph.expected} 个原生点 · 左轴电流 nA / 右轴 E (mV)`
      : '左轴电流 nA · 右轴 E = V_WE − V_RE (mV)';
  $('dbgChartEmpty').textContent = inQuiet
    ? `静置中,还剩 ${quietLeft} s 才开始记录电流（电位曲线已在走）`
    : running ? '等待第一个样本…' : '点「应用并开始一次 I-t」后显示';
  // 🔴 命令只能在测量进行中下发(RTT 下行通道由采集器持有)⇒ 不跑就禁用
  ['dbgGet', 'dbgOcp'].forEach(id => { $(id).disabled = !running; });
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

  // ── 实时电流(用户明确要的:界面上必须有电流值,不能只有图)──────────────
  const cur = d.series?.current || {t: [], nA: [], valid: [], ep: []};
  const n = cur.nA.length;
  if (n) {
    const last = cur.nA[n - 1], lastOk = cur.valid[n - 1] !== false;
    $('dbgLiveI').textContent = fmt(last, 3);
    $('dbgLiveIAt').textContent = `t = ${fmt(cur.t[n - 1], 2)} s · ep ${cur.ep[n - 1] ?? '?'}`
      + (lastOk ? '' : ' · 🔴 饱和,该点不是测量');
    $('dbgLiveIBox').classList.toggle('invalid', !lastOk);
    const win = cur.nA.slice(-20).filter(Number.isFinite);
    const sorted = [...win].sort((a, b) => a - b);
    const med = sorted[Math.floor(sorted.length / 2)];
    const mean = win.reduce((a, b) => a + b, 0) / win.length;
    const sd = Math.sqrt(win.reduce((a, b) => a + (b - mean) ** 2, 0) / win.length);
    $('dbgIMed').textContent = fmt(med, 3);
    $('dbgISd').textContent = fmt(sd, 3);
    const nsat = cur.valid.filter(v => v === false).length;
    $('dbgN').textContent = String(n);
    $('dbgSat').textContent = nsat ? `🔴 sat ${nsat}` : 'sat 0';
    $('dbgSat').style.color = nsat ? 'var(--red)' : '';
  } else {
    ['dbgLiveI','dbgIMed','dbgISd','dbgN'].forEach(i => { $(i).textContent = '--'; });
    $('dbgLiveIAt').textContent = '尚无数据';
    $('dbgSat').textContent = 'sat 0';
  }
  $('dbgCounts').textContent = cfg.lsb_eff_fa
    ? `${fmt(cfg.lsb_eff_fa / 1000, 3)} pA/码` : '--';
  $('dbgCountsNote').textContent = cfg.bits ? `${cfg.bits} bit 有效台阶` : '原始码';
  renderDbgReject(d.last_reject);

  $('dbgWe').textContent = fmt(c.we_mv, 0); $('dbgRe').textContent = fmt(c.re_mv, 0);
  $('dbgCe').textContent = fmt(c.ce_mv, 0); $('dbgWo').textContent = fmt(c.wo_mv, 0);
  $('dbgLiveE').textContent = c.e_mv == null ? '--' : fmt(c.e_mv, 0);
  $('dbgLiveEAt').textContent = c.rows ? `${c.rows} 组 · dev ${fmt(c.dev_ms / 1000, 1)} s` : '尚无数据';
  $('dbgLiveBox').classList.toggle('invalid', Boolean(c.clipped));
  // 🔴 原始 code 必须显示:整池对芯片 GND 浮动时电压会撞 0/4095,只看 mV 看不出削顶
  $('dbgCodes').textContent = c.we_code == null ? '原始 12-bit code:等待数据'
    : `原始 12-bit code — WE ${c.we_code} · RE ${c.re_code} · CE ${c.ce_code} · WO ${c.wo_code}`
      + (c.clipped ? '　⚠️ 有 code 撞 0 或 4095,该电位读数不可信' : '');

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
  drawDebug();
}

function drawDebug() {
  const s = state.debug?.series || {};
  const cur = s.current || {t: [], nA: [], valid: []}, cv = s.cell_v || {t: [], e_mv: [], clipped: []};
  const curPts = [], curBad = [], ePts = [], eBad = [];
  (cur.t || []).forEach((t, i) => {
    curPts.push([t, cur.nA[i]]);
    if (cur.valid?.[i] === false) curBad.push([t, cur.nA[i]]);
  });
  (cv.t || []).forEach((t, i) => {
    ePts.push([t, cv.e_mv[i]]);
    if (cv.clipped?.[i]) eBad.push([t, cv.e_mv[i]]);
  });
  const latest = Math.max(curPts.at(-1)?.[0] || 0, ePts.at(-1)?.[0] || 0);
  const win = state.dbgChartWindowS;
  const xmin = win === null ? 0 : Math.max(0, latest - win);
  const xmax = win === null ? Math.max(1, latest) : Math.max(win, latest);
  const vis = pts => pts.filter(p => p[0] >= xmin && p[0] <= xmax);
  $('dbgChartEmpty').hidden = curPts.length > 0 || ePts.length > 0;
  drawChart($('dbgChart'), [
    {points: vis(curPts), color: '#167b74', width: 1.4},
    {points: vis(curBad), color: '#c33c54', width: 0, dots: true, pointRadius: 1.8},
    {points: vis(ePts), color: '#8a6fb0', width: 1.4, axis: 'right'},
    {points: vis(eBad), color: '#c33c54', width: 0, dots: true, pointRadius: 2.4, axis: 'right'},
  ], {xmin, xmax, xlabel: '时间 (s，设备时钟)', ylabel: '电流 (nA)',
      y2label: 'E = V_WE − V_RE (mV)', yDigits: 2, y2Digits: 0});
}

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
  if (state.dbgCfgSeen !== cfg.ep) { state.dbgCfgSeen = cfg.ep; state.dbgDirty.clear(); }
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
function renderDbgApply(d) {
  const running = d?.state === 'running';
  const cfg = d?.cfg || {};
  // 🔴 还没读到设备配置时,控件里是 HTML 默认值而**不是**设备真值。
  //    此时绝不能把表单当成"要应用的配置"下发 —— 那会把猜的值(FSR 50nA、E 空)
  //    写进硬件。改成:这一按只起一轮,让 auto-GET 把真值读回来填表。
  const known = cfg.fsr !== undefined;
  const changed = known ? dbgDiffKeys(cfg) : [];
  const btn = $('dbgApply');
  btn.classList.toggle('warn-btn', running && changed.length > 0);
  btn.textContent = !known ? '开始一次 I-t（先读回设备配置）'
    : running ? (changed.length ? '应用（测量中强制改写）' : '重新下发（测量中）')
    : '应用并开始一次 I-t';
  btn.title = !known ? '尚未读到设备配置,本次只起一轮并读回真值,不会写任何参数'
    : running ? '本轮正在采集:改这些参数会扰动电解池,本轮会被标 tainted,数据不得用于标定'
    : '先把参数写进硬件,再立即开始一次 I-t 测量';
  $('dbgStop').disabled = !running;
  $('dbgDiff').textContent = !known
    ? '尚未读到设备配置（控件里现在是页面默认值,不是硬件真值）'
    : changed.length ? `将改动 ${changed.length} 项：${changed.join(' · ')}`
    : '与设备当前配置一致';
  $('dbgDiff').classList.toggle('changed', known && changed.length > 0);
}

async function dbgSend(line) {
  try {
    $('dbgError').hidden = true;
    await post('/api/debug/cmd', {line});
    toast(`已下发：${line.length > 60 ? line.slice(0, 60) + '…' : line}`);
    setTimeout(refreshDebug, 500);
  } catch (e) { errorBox('dbgError', e); }
}
async function refreshDebug() {
  try { renderDebug(await api('/api/debug')); }
  catch { $('dbgStatusBadge').textContent = '服务未连接'; }
}

$('dbgChartWindow').addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button) return;
  $('dbgChartWindow').querySelectorAll('button').forEach(x => x.classList.toggle('active', x === button));
  state.dbgChartWindowS = button.dataset.window === 'all' ? null : Number(button.dataset.window);
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
  const known = state.debug?.cfg?.fsr !== undefined;
  const line = dbgComposeSet(running);   // 测量中一律带 FORCE(按钮已变黄示警)
  try {
    $('dbgError').hidden = true;
    if (!running) {
      // 🔴 次序:先起一轮,再下发参数。命令只能经采集器的下行通道走
      //    (JLinkExe 只转发采集器持有的那个连接),没有 run 就无处可发。
      //    而参数会落在 Quiet Time 里 —— 那时 acquiring=false,扰动键无需 FORCE、
      //    本轮也不会被标 tainted,正好赶在记录开始之前生效。
      await post('/api/debug/start', {note: 'hw-debug'});
      if (!known) {
        // 不知道设备现在是什么 ⇒ 什么都不写,只等 auto-GET 把真值读回来
        toast('本轮已开始;正在读回设备配置…');
        setTimeout(refreshDebug, 1500);
        return;
      }
      await new Promise(r => setTimeout(r, 1200));   // 等采集器起来并接上下行
    }
    await post('/api/debug/cmd', {line});
    state.dbgDirty.clear();
    toast(running ? '已在测量中强制改写参数（本轮已标 tainted）' : '参数已下发,本轮开始');
    setTimeout(refreshDebug, 600);
  } catch (e) { errorBox('dbgError', e); }
});
$('dbgStop').addEventListener('click', async () => {
  try { await post('/api/debug/stop'); await refreshDebug(); }
  catch (e) { errorBox('dbgError', e); }
});
setInterval(refreshDebug,1000);
window.addEventListener('resize',()=>{drawAll();drawDebug()});
init();
