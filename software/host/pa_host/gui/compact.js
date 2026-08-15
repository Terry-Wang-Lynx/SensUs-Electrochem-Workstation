const $ = id => document.getElementById(id);
const state = {
  measurement: null,
  settings: null,
  workflow: null,
  role: localStorage.getItem('compact-role') || 'calibration',
  refreshing: false
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {'Content-Type': 'application/json'},
    cache: 'no-store',
    ...options
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function post(path, body = {}) {
  return api(path, {method: 'POST', body: JSON.stringify(body)});
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

function setRole(role) {
  if (role === 'test' && !state.workflow?.calibration_ready) {
    setMessage('请先在完整工作站中生成标定曲线', true);
    return;
  }
  state.role = role;
  localStorage.setItem('compact-role', role);
  document.querySelectorAll('#sampleRole button').forEach(button => {
    button.classList.toggle('active', button.dataset.role === role);
  });
  $('concentration').placeholder = role === 'calibration' ? '必填' : '可选';
  updateButton();
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
  if (state.role === 'test' && !payload.calibration_ready) setRole('calibration');
  updateButton();
}

function renderMeasurement(payload) {
  state.measurement = payload;
  const running = payload.state === 'running';
  const errored = payload.state === 'error';
  const complete = payload.state === 'completed';
  $('statusText').textContent = running ? '正在测量' : errored ? '采集异常' : complete ? '测量完成' : '硬件待测';
  $('statusDot').className = 'status-dot ' + (running ? 'running' : errored ? 'error' : '');

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
    const cycle = Math.max(0, ...(data.cycle || []));
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

  let xmin = Math.min(...points.map(point => point.x));
  let xmax = Math.max(...points.map(point => point.x));
  let ymin = Math.min(...points.map(point => point.y));
  let ymax = Math.max(...points.map(point => point.y));
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
  const settingsApplied = state.settings?.applied;
  const cv = state.settings?.settings?.method === 'cv';
  const concentrationMissing = state.role === 'calibration' && $('concentration').value === '';
  const testUnavailable = state.role === 'test' && !state.workflow?.calibration_ready;
  const nameMissing = $('sampleName').value.trim() === '';
  const button = $('measureButton');
  button.classList.toggle('stop', running);
  button.textContent = running
    ? '停止测量'
    : cv ? '开始 CV 扫描' : state.role === 'calibration' ? '开始标定测量' : '开始测试并预测';
  button.disabled = running
    ? false
    : !settingsApplied || !state.workflow?.save_dir || nameMissing || (!cv && (concentrationMissing || testUnavailable));
}

async function toggleMeasurement() {
  try {
    if (state.measurement?.state === 'running') {
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
    }));
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const measurement = await api('/api/status');
    renderMeasurement(measurement);
    if (measurement.state !== 'running') {
      renderWorkflow(await api('/api/workflow'));
      renderSettings(await api('/api/settings'));
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
window.addEventListener('resize', () => {
  if (state.measurement) drawChart(state.measurement);
});

setRole(state.role);
Promise.all([api('/api/settings'), api('/api/workflow'), api('/api/status')])
  .then(([settings, workflow, measurement]) => {
    renderSettings(settings);
    renderWorkflow(workflow);
    renderMeasurement(measurement);
    refresh();
  })
  .catch(error => setMessage(error.message, true));
