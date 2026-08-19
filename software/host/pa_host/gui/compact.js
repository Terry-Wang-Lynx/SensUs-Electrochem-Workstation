const $ = id => document.getElementById(id);
const state = {
  measurement: null,
  settings: null,
  workflow: null,
  devices: {devices: [], selected_device_id: null, busy: false, probing: true},
  role: localStorage.getItem('compact-role') || 'calibration',
  refreshing: false,
  measurementAction: null
};
const MEASUREMENT_START_TIMEOUT_MS = 180000;

async function api(path, options = {}) {
  const {timeoutMs = 5000, ...fetchOptions} = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      headers: {'Content-Type': 'application/json'},
      cache: 'no-store',
      ...fetchOptions,
      signal: controller.signal
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
  } catch (error) {
    if (error?.name === 'AbortError') throw new Error('请求超时，设备正在重新连接');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function post(path, body = {}, timeoutMs = 5000) {
  return api(path, {method: 'POST', body: JSON.stringify(body), timeoutMs});
}

function fmt(value, digits = 3) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '--';
}

function signed(value) {
  const number = Number(value);
  return (number > 0 ? '+' : '') + number.toFixed(2);
}

function setMessage(text, error = false) {
  $('message').textContent = text || '';
  $('message').classList.toggle('error', error);
}

/*
 * 🔴 2026-08-19 修:启动时序竞态导致「界面显示标定、内部状态是测试」。
 *
 * 旧实现在 role==='test' 且标定未就绪时**直接 return** —— 而启动序列是
 *     state.role = localStorage('compact-role')   // 可能是 'test'
 *     setRole(state.role)                          // 此刻 state.workflow 还是 null
 *     Promise.all([...settings, workflow...])      // workflow 之后才到
 * 于是上次停在"测试"的用户重开小窗时必然命中那个 return:state.role 仍是 'test',
 * 但按钮高亮停在 HTML 默认的"标定"。后果是 updateButton() 里
 * `state.role==='calibration'` 判为 false ⇒ **标定模式下浓度留空也允许开始**,
 * 且提交时 sample_role='test' ⇒ 该点不进标定集。用户反馈的"调浓度代码错乱"就是它。
 *
 * 现在:拒绝切换时也**把 UI 同步回真实的 state.role**,保证两者永不分叉。
 */
function setRole(role, options = {}) {
  const {quiet = false} = options;
  if (role === 'test' && !state.workflow?.calibration_ready) {
    if (!quiet) setMessage('请先在完整工作站中生成标定曲线', true);
    // 回落到标定,而不是留下 UI/state 不一致
    if (state.role !== 'calibration') {
      state.role = 'calibration';
      localStorage.setItem('compact-role', 'calibration');
    }
    syncRoleUi();
    updateButton();
    return;
  }
  state.role = role;
  localStorage.setItem('compact-role', role);
  syncRoleUi();
  updateButton();
}

/* UI 与 state.role 的唯一同步点 —— 任何改 role 的路径都必须过这里 */
function syncRoleUi() {
  document.querySelectorAll('#sampleRole button').forEach(button => {
    button.classList.toggle('active', button.dataset.role === state.role);
  });
  $('concentration').placeholder = state.role === 'calibration' ? '必填' : '可选';
}

function renderSettings(payload) {
  state.settings = payload;
  const settings = payload.settings || {};
  const method = settings.method === 'cv' ? 'CV' : 'I-T';
  const potential = settings.method === 'cv'
    ? signed(settings.cv_low_v) + '–' + signed(settings.cv_high_v) + ' V'
    : signed(settings.potential_v) + ' V';
  const range = Number(settings.fsr_nA) > 2000
    ? Number(settings.fsr_nA) / 1000 + ' µA'
    : settings.fsr_nA + ' nA';
  $('conditionText').textContent = method + ' · ' + potential + ' · ' + range;
  $('sampleControls').hidden = settings.method === 'cv';
  updateButton();
}

function renderWorkflow(payload) {
  state.workflow = payload;
  const testButton = document.querySelector('[data-role="test"]');
  testButton.disabled = !payload.calibration_ready;
  if (state.role === 'test' && !payload.calibration_ready) {
    // 恢复出来的 role 不可用 ⇒ 拉回标定(quiet:启动时不该弹错误提示)
    setRole('calibration', {quiet: true});
  } else {
    // 🔴 role 可用时也必须同步一次 UI —— 否则启动时若 role='test' 且标定已就绪,
    //    按钮高亮会一直停在 HTML 默认的"标定",与 state 分叉(旧 bug 的另一半)。
    syncRoleUi();
  }
  updateButton();
}

function renderDevices(payload) {
  state.devices = payload || {devices: [], selected_device_id: null, busy: false, probing: false};
  updateButton();
}

function hardwareOperationStatus() {
  const payload = state.devices || {};
  const devices = Array.isArray(payload.devices) ? payload.devices : [];
  const usable = devices.filter(device => device.selectable);
  const selected = payload.selected_device
    || devices.find(device => device.id === payload.selected_device_id)
    || null;
  if (selected?.present === false) return {ready: false, message: '所选设备已断开'};
  const active = selected || (usable.length === 1 ? usable[0] : null);
  if (!active) {
    if (usable.length > 1) return {ready: false, message: '请在完整工作站中选择本次使用的设备'};
    return {ready: false, message: payload.probing ? '正在核对硬件连接' : '未发现可用硬件'};
  }
  if (active.kind === 'jlink') {
    if (active.target_state === 'reachable') return {ready: true, message: ''};
    if (active.driver_state === 'missing') return {ready: false, message: '请在完整工作站中准备 J-Link Windows 驱动'};
    return {
      ready: false,
      message: active.target_state === 'unreachable'
        ? String(active.target_detail || '目标板无响应，请检查板卡供电和 SWD 排线')
        : '正在核对 J-Link 目标板'
    };
  }
  return active.selectable
    ? {ready: true, message: ''}
    : {ready: false, message: 'USB DATA 与 SMP 接口尚未就绪'};
}

function renderMeasurement(payload) {
  state.measurement = payload;
  const running = payload.state === 'running';
  const starting = state.measurementAction === 'starting'
    || payload.operation_phase === 'configuring'
    || payload.config_gate?.state === 'checking';
  const errored = payload.state === 'error';
  const complete = payload.state === 'completed';
  $('statusText').textContent = starting ? '正在核对硬件配置' : running ? '正在测量' : errored ? '采集异常' : complete ? '测量完成' : '硬件待测';
  $('statusDot').className = 'status-dot ' + (running&&!starting ? 'running' : errored ? 'error' : '');

  const settings = payload.settings || state.settings?.settings || {};
  const cv = settings.method === 'cv';
  const latest = payload.latest_sample;
  if (latest) {
    $('liveCurrent').textContent = fmt(cv ? latest.current_nA / 1000 : latest.current_nA);
    $('liveUnit').textContent = cv ? 'µA' : 'nA';
    $('liveMeta').textContent = cv
      ? fmt(latest.potential_v, 3) + ' V · 第 ' + (latest.cycle || '--') + ' 圈 · 点 ' + (Number(latest.index) + 1)
      : 't = ' + fmt(latest.time_s, 2) + ' s · 点 ' + (Number(latest.index) + 1);
  } else {
    $('liveCurrent').textContent = '--';
    $('liveMeta').textContent = '尚无实时数据';
  }

  const metrics = payload.rolling_metrics || {};
  const adaptive = !cv && Boolean(settings.adaptive_stop);
  const progress = Number.isFinite(Number(metrics.progress_percent))
    ? Math.min(100, Math.max(0, Number(metrics.progress_percent))) : 0;
  const nativePoints = Number.isFinite(Number(metrics.native_point_count))
    ? Number(metrics.native_point_count) : 0;
  $('progressBar').style.width = progress + '%';
  $('progressText').textContent = adaptive
    ? String(payload.stability_eta?.display_text || '正在估计') + ` · ${nativePoints}点`
    : `${Math.round(progress)}% · ${nativePoints}点`;

  const result = payload.workflow_result;
  if (result?.predicted_concentration_um !== null && result?.predicted_concentration_um !== undefined) {
    setMessage('预测浓度：' + fmt(result.predicted_concentration_um) + ' µM');
  } else {
    setMessage(payload.error || payload.message || '', Boolean(payload.error));
  }
  drawChart(payload);
  updateButton();
}

function drawChart(payload) {
  if (document.visibilityState === 'hidden') return;
  const canvas = $('liveChart');
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const data = payload.data || {};
  const current = data.current_nA || [];
  if (!current.length) {
    $('chartEmpty').hidden = false;
    return;
  }
  $('chartEmpty').hidden = true;
  const cv = payload.settings?.method === 'cv';
  let points;
  if (cv) {
    let cycle = 0;
    for (const value of (data.cycle || [])) cycle = Math.max(cycle, Number(value) || 0);
    points = (data.potential_v || []).map((x, index) => ({
      x,
      y: current[index] / 1000,
      valid: data.valid?.[index] !== false,
      cycle: data.cycle?.[index]
    })).filter(point => point.cycle === cycle);
  } else {
    const times = data.time_s || [];
    const latest = Number(times.at(-1) || 0);
    const first = Math.max(0, latest - 30);
    points = times.map((x, index) => ({
      x,
      y: current[index],
      valid: data.valid?.[index] !== false
    })).filter(point => point.x >= first);
  }
  if (!points.length) return;

  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  for (const point of points) {
    xmin = Math.min(xmin, point.x); xmax = Math.max(xmax, point.x);
    ymin = Math.min(ymin, point.y); ymax = Math.max(ymax, point.y);
  }
  if (xmin === xmax) xmax = xmin + 1;
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const ypad = (ymax - ymin) * 0.12;
  ymin -= ypad;
  ymax += ypad;

  const margin = {left: 41, right: 8, top: 9, bottom: 21};
  const xpx = value => margin.left + (value - xmin) / (xmax - xmin) * (rect.width - margin.left - margin.right);
  const ypx = value => margin.top + (ymax - value) / (ymax - ymin) * (rect.height - margin.top - margin.bottom);
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;

  ctx.font = '9px -apple-system';
  ctx.lineWidth = 1;
  ctx.strokeStyle = dark ? '#344548' : '#e0e7e7';
  ctx.fillStyle = '#7b898c';
  for (let index = 0; index <= 3; index += 1) {
    const y = ymin + (ymax - ymin) * index / 3;
    const py = ypx(y);
    ctx.beginPath();
    ctx.moveTo(margin.left, py);
    ctx.lineTo(rect.width - margin.right, py);
    ctx.stroke();
    ctx.fillText(y.toFixed(1), 3, py + 3);
  }

  ctx.strokeStyle = '#16866f';
  ctx.lineWidth = 1.1;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index) ctx.lineTo(xpx(point.x), ypx(point.y));
    else ctx.moveTo(xpx(point.x), ypx(point.y));
  });
  ctx.stroke();

  points.forEach(point => {
    ctx.fillStyle = point.valid ? '#16866f' : '#c14a45';
    ctx.beginPath();
    ctx.arc(xpx(point.x), ypx(point.y), 1.5, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = '#7b898c';
  ctx.fillText(cv ? '电位 (V)' : '时间 (s)', rect.width / 2 - 18, rect.height - 4);
}

function updateButton() {
  const running = state.measurement?.state === 'running';
  const starting = state.measurementAction === 'starting'
    || state.measurement?.operation_phase === 'configuring'
    || state.measurement?.config_gate?.state === 'checking';
  const stopping = state.measurementAction === 'stopping';
  const settingsApplied = state.settings?.applied;
  const workspaceAvailable = state.workflow?.workspace_available ?? Boolean(state.workflow?.save_dir);
  const hardware = hardwareOperationStatus();
  const cv = state.settings?.settings?.method === 'cv';
  const concentrationMissing = state.role === 'calibration' && $('concentration').value === '';
  const testUnavailable = state.role === 'test' && !state.workflow?.calibration_ready;
  const nameMissing = $('sampleName').value.trim() === '';
  const button = $('measureButton');
  button.classList.toggle('stop', running&&!starting);
  button.textContent = starting
    ? '正在核对硬件配置…'
    : stopping ? '正在停止…' : running
    ? '停止测量'
    : cv ? '开始 CV 扫描' : state.role === 'calibration' ? '开始标定测量' : '开始测试并预测';
  button.disabled = starting || stopping || Boolean(state.devices?.busy) ? true : running
    ? false
    : !hardware.ready || !settingsApplied || !workspaceAvailable || nameMissing || (!cv && (concentrationMissing || testUnavailable));
  button.title = !starting&&!stopping&&!running&&!hardware.ready ? hardware.message : '';
}

async function toggleMeasurement() {
  if (state.measurementAction) return;
  const stopping = state.measurement?.state === 'running';
  state.measurementAction = stopping ? 'stopping' : 'starting';
  updateButton();
  try {
    if (stopping) {
      renderMeasurement(await post('/api/measurement/stop'));
      return;
    }
    const concentration = $('concentration').value;
    renderMeasurement(await post('/api/measurement/start', {
      sample_name: $('sampleName').value.trim(),
      known_concentration_um: concentration === '' ? null : concentration,
      sample_role: state.role,
      save_dir: state.workflow?.save_dir || '',
      source: 'compact_overlay'
    }, MEASUREMENT_START_TIMEOUT_MS));
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    state.measurementAction = null;
    updateButton();
  }
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const measurement = await api('/api/status', {timeoutMs: 1500});
    renderMeasurement(measurement);
    if (measurement.state !== 'running') {
      const [workflow, settings, devices] = await Promise.all([
        api('/api/workflow'), api('/api/settings'), api('/api/devices', {timeoutMs: 3000})
      ]);
      renderWorkflow(workflow);
      renderSettings(settings);
      renderDevices(devices);
    }
  } catch (error) {
    $('statusText').textContent = '服务未连接';
    $('statusDot').className = 'status-dot error';
    setMessage(error.message, true);
  } finally {
    state.refreshing = false;
    setTimeout(refresh, state.measurement?.state === 'running' ? 100 : 1000);
  }
}

$('sampleName').value = localStorage.getItem('compact-sample-name') || '';
$('concentration').value = localStorage.getItem('compact-concentration') || '';
$('sampleName').addEventListener('input', () => {
  localStorage.setItem('compact-sample-name', $('sampleName').value);
  updateButton();
});
$('concentration').addEventListener('input', () => {
  localStorage.setItem('compact-concentration', $('concentration').value);
  updateButton();
});
$('sampleRole').addEventListener('click', event => {
  const button = event.target.closest('button[data-role]');
  if (button) setRole(button.dataset.role);
});
$('measureButton').addEventListener('click', toggleMeasurement);
let resizeTimer = null;
let resizeRedrawForced = false;
let lastChartLayout = '';
function scheduleChartRedraw(force = false) {
  resizeRedrawForced = resizeRedrawForced || force;
  if (resizeTimer !== null) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    resizeTimer = null;
    const forced = resizeRedrawForced;
    resizeRedrawForced = false;
    if (document.visibilityState === 'hidden' || !state.measurement) return;
    const canvas = $('liveChart'), rect = canvas.getBoundingClientRect();
    const layout = `${Math.round(rect.width * 100)}x${Math.round(rect.height * 100)}@${window.devicePixelRatio || 1}`;
    if (!forced && layout === lastChartLayout) return;
    lastChartLayout = layout;
    requestAnimationFrame(() => drawChart(state.measurement));
  }, 120);
}
window.addEventListener('resize', () => scheduleChartRedraw(), {passive: true});
window.addEventListener('orientationchange', () => scheduleChartRedraw(true), {passive: true});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    scheduleChartRedraw(true);
    void refresh();
  }
});

/*
 * 🔴 启动时先只同步 UI,**不做可用性判定** —— 此刻 state.workflow 还是 null,
 * 任何 role==='test' 的判定都必然误判。等 renderWorkflow 拿到真实的
 * calibration_ready 后再由它裁决(它会在不就绪时把 role 拉回标定)。
 */
syncRoleUi();
Promise.all([api('/api/settings'), api('/api/workflow'), api('/api/status'), api('/api/devices', {timeoutMs: 3000})])
  .then(([settings, workflow, measurement, devices]) => {
    renderSettings(settings);
    renderWorkflow(workflow);   // ← 这里才裁决恢复出来的 role 是否可用
    renderDevices(devices);
    renderMeasurement(measurement);
    refresh();
  })
  .catch(error => setMessage(error.message, true));
