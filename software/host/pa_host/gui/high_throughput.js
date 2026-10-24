(function (scope) {
  'use strict';
  const CHANNELS = 'ABCDEFGH'.split('');
  const API_ROOT = '/api/automation/v1/';
  const FRONTEND_BUILD = '2026-10-24 · endpoint-approach-r1';
  const copy = value => JSON.parse(JSON.stringify(value));
  const failure = (code, message, path = '') => Object.assign(new Error(message), {code, path});
  const SAMPLING_DEFAULTS = {potential_v: -0.2, working_electrode_v: 0.4, sens_period_code: 0, fsr_nA: 50, offset_mode: '20pct'};
  const SAMPLING_FIELDS = Object.keys(SAMPLING_DEFAULTS);
  const SAMPLING_OFFSETS = ['10nA', '20nA', '40nA', '80nA', '10pct', '20pct', '50pct'];
  const LIMIT_BOUNDARIES = [['x_left', 'X 最左'], ['x_right', 'X 最右'], ['z_up', 'Z 最高'], ['z_down', 'Z 最低']];
  const CALIBRATION_POINTS = [['first', '第 1 列 X'], ['last', '第 12 列 X'], ['working', '浸入工作 Z'], ['safe', '提出安全 Z']];

  function sharedSamplingParameters(document) {
    const parameters = slot => Object.fromEntries(SAMPLING_FIELDS.map(field => [field, document.bindings?.boards?.[slot]?.settings?.[field] ?? SAMPLING_DEFAULTS[field]]));
    const settings = parameters('A'), other = parameters('B');
    const differences = SAMPLING_FIELDS.filter(field => settings[field] !== other[field]);
    return {shared: differences.length === 0, differences, settings};
  }

  function validateSamplingParameters(settings) {
    const invalid = (message, path) => { throw failure('SAMPLING_INVALID', message, path); };
    for (const field of SAMPLING_FIELDS.filter(field => field !== 'offset_mode')) if (!Number.isFinite(settings[field])) invalid('采样参数须为有限数值', field);
    if (settings.potential_v < -0.4 || settings.potential_v > 0.4) invalid('测试电位须在 −0.4 至 +0.4 V 之间', 'potential_v');
    if (settings.working_electrode_v < 0.25 || settings.working_electrode_v > 1.535) invalid('WE 电位须在 0.25 至 1.535 V 之间', 'working_electrode_v');
    const reference = settings.working_electrode_v - settings.potential_v;
    if (reference < 0.1 || reference > 1.535625) invalid('该 WE / 测试电位组合对应的 RE 超出 0.1 至 1.535625 V 范围', 'potential_v');
    if (!Number.isInteger(settings.sens_period_code) || settings.sens_period_code < 0 || settings.sens_period_code > 10) invalid('V5.2 采样周期须为 PERIOD 0–10', 'sens_period_code');
    if (![50, 100, 250, 500, 1000, 2000].includes(settings.fsr_nA)) invalid('请选择 V5.2 DC 电流量程', 'fsr_nA');
    if (!SAMPLING_OFFSETS.includes(settings.offset_mode)) invalid('请选择 V5.2 偏置电流', 'offset_mode');
    const offset = settings.offset_mode.endsWith('pct') ? settings.fsr_nA * Number(settings.offset_mode.slice(0, -3)) / 100 : Number(settings.offset_mode.slice(0, -2));
    if (offset >= settings.fsr_nA) invalid('偏置电流须小于所选量程', 'offset_mode');
    return Object.fromEntries(SAMPLING_FIELDS.map(field => [field, settings[field]]));
  }

  function boardElectrodeMapping(document) {
    return ['A', 'B'].map((slot, offset) => ({slot, address: document.bindings?.boards?.[slot]?.address || '',
      channels: CHANNELS.slice(offset * 4, offset * 4 + 4).map((electrode, index) => ({electrode, channel: index + 1}))}));
  }

  function columnDurationCaption(document, index) {
    const column = document.columns.find(item => item.index === index), template = document.defaults.dwell_s;
    const inherited = column.duration_s == null, duration = inherited ? template : column.duration_s;
    return {effective_s: duration, inherited, label: '本列覆盖时长（留空继承 ' + template + ' s）',
      summary: inherited ? '本列实际 ' + duration + ' s · 继承模板默认' : '本列实际 ' + duration + ' s · 本列覆盖；模板默认 ' + template + ' s 不影响此列'};
  }

  function calibrationReferenceCaption(calibration) {
    if (calibration?.usable === true) return '当前参考可用';
    return calibration?.status === 'stale' ? '当前参考已失效，需主动处理标定' : '当前参考未确认，实物实验受阻';
  }

  function errorGuidance(error, manual = {}, controller = {}) {
    const code = String(error?.code || '').toLowerCase();
    const rejection = String((error?.wire || error?.cause?.wire)?.pending_hex || '').match(/^([0-9a-f]{2})00ee6b$/i);
    const motorFault = rejection ? '地址 ' + parseInt(rejection[1], 16) + ' 电机返回“命令格式错误”。' : '';
    if (code === 'jog_stopped_fault') {
      return motorFault + '双轴已确认停止，但本次键盘会话因原始故障中断。请点击「重连并重置」重新检测设备，再进入标定；不会自动续动。';
    }
    const canRestart = controller.recovery_required !== true && controller.stop_confirmed !== false
      && (controller.stop_confirmed === true || manual.alignment?.stop_result?.stop_confirmed === true);
    const restart = canRestart
      ? '若要继续标定，请先松开所有方向键，确认现场安全并勾选本次运动确认，再点击「重新开始键盘标定（旧点作废）」。软件会先复核双轴停止，再重新检查电机；旧点位不能沿用。'
      : '双轴停止尚未确认。若要重新标定，请先松开方向键，检查电机供电与 RS485 连线，再点击「停止」；确认停止后勾选本次运动确认，再点击「重新开始键盘标定（旧点作废）」。';
    if (['manual_calibration_active', 'calibration_active'].includes(code)) {
      return manual.status === 'blocked' ? '上一次标定已中断。' + restart
        : '当前标定尚未结束。若要继续对位，请使用 WASD / 方向键并读取四个标定点；若要重新开始或修改实验配置，请先点击「取消标定」，待停止确认后再操作。';
    }
    if (['manual_target_mismatch', 'manual_relock_target'].includes(code)) {
      return '电机记住的目标与当前位置不一致，重新使能可能突然移动。若要进入键盘标定，请先关闭两台电机的电源（不是只拔 USB），重新通电后不要手拧轴，再确认现场安全并点击「重新开始键盘标定（旧点作废）」。';
    }
    if (['alignment_stop_unknown', 'alignment_stop_unconfirmed', 'stop_unconfirmed'].includes(code)) {
      return motorFault + '软件尚未确认双轴停止，不能继续对位或实验。请先松开方向键，在现场确认电机状态，检查供电与 RS485 连线，再点击「停止」；若仍无法确认，请先现场断电停机，不要反复点击开始。';
    }
    if (['response_length', 'response_timeout', 'transaction_timeout', 'response_checksum', 'response_address', 'response_opcode', 'serial_error', 'motion_disconnected', 'motion_required', 'reconnect_required', 'not_connected'].includes(code)) {
      return motorFault + '电机连接或通信异常，本次操作未完成。' + (manual.active ? restart
        : '若要开始标定，请先检查两台电机供电与 RS485 连线，在「高级设置」选择并保存共享串口，再点击「连接并开始键盘标定」。');
    }
    if (['alignment_unavailable', 'alignment_session', 'manual_interrupted', 'calibration_recovery_required'].includes(code)) return '原来的键盘标定会话已不能继续。' + restart;
    if (code === 'calibration_points' || code === 'calibration_points_required') return '若要完成标定，请先分别对齐并读取「第 1 列 X」「第 12 列 X」「浸入工作 Z」「提出安全 Z」四个点，再点击「完成四点标定」。';
    if (code === 'busy' || code === 'hardware_busy') return '已有任务正在占用设备，或停止尚未确认。若要开始标定，请先点击「停止」，等待当前任务结束并确认停止，再重新进入标定。';
    const message = error?.message || String(error);
    return /[\u3400-\u9fff]/.test(message) ? message : '本次操作未完成。请先检查设备状态；若仍无法继续，请展开「技术详情」，把其中的信息提供给维护人员。';
  }

  function createModel(initial) {
    const model = {
      document: copy(initial), selectedColumn: 1, plan: null, compiledDocument: null,
      serialize() { return JSON.stringify(this.document); },
      selectColumn(index) {
        if (!Number.isInteger(index) || index < 1 || index > 12) throw failure('COLUMN_INVALID', '列号须为 1–12');
        this.selectedColumn = index;
      },
      invalidate() { this.plan = null; this.compiledDocument = null; },
      edit(change) {
        const next = copy(this.document);
        change(next);
        this.document = next;
        this.invalidate();
      },
      replace(next) {
        if (!next || typeof next !== 'object' || Array.isArray(next) || !Array.isArray(next.columns) || !Array.isArray(next.program)) {
          throw failure('DOCUMENT_INVALID', '实验 JSON 必须包含 columns 和 program 数组');
        }
        this.document = copy(next);
        this.invalidate();
      },
      updateColumn(index, values) {
        this.edit(document => {
          let column = document.columns.find(item => item.index === index);
          if (!column) {
            column = {index, solution: '', concentration: {value: 0, unit: 'uM'}, overrides: {}};
            document.columns.push(column);
            document.columns.sort((left, right) => left.index - right.index);
          }
          Object.assign(column, copy(values));
        });
      },
      acceptPlan(plan, requestedDocument) {
        if (requestedDocument !== this.serialize()) throw failure('STALE_PLAN', '预览期间文档已变更，请重新编译');
        if (!plan?.plan_hash) throw failure('PLAN_INVALID', '服务未返回计划指纹');
        this.plan = copy(plan);
        this.compiledDocument = requestedDocument;
      },
      runPayload(mode, liveConfirmed, key) {
        if (!this.plan || this.compiledDocument !== this.serialize()) throw failure('PREVIEW_REQUIRED', '请先编译当前实验');
        if (!['simulate', 'live'].includes(mode)) throw failure('MODE_INVALID', '运行模式无效');
        if (mode === 'live' && !liveConfirmed) throw failure('LIVE_CONFIRMATION_REQUIRED', '请明确确认本次实物运行');
        if (mode === 'live' && !this.document.safety?.motion_policy_confirmed) throw failure('MOTION_POLICY_REQUIRED', '请先确认运动策略');
        if (mode === 'live' && !this.document.safety?.electrical_policy_confirmed) throw failure('ELECTRICAL_POLICY_REQUIRED', '请先确认电极极化策略');
        return {experiment: copy(this.document), mode, plan_hash: this.plan.plan_hash, idempotency_key: key};
      },
    };
    return model;
  }

  function actionFor(op, column, channels) {
    if (op === 'move_to_column') return {op, column};
    if (op === 'immerse' || op === 'withdraw' || op === 'wait_operator') return {op};
    if (op === 'dwell') return {op, acquire: CHANNELS.filter(id => channels.includes(id))};
    if (op === 'wait') return {op, seconds: 2};
    if (op === 'repeat') return {op, count: 2, body: [{op: 'wait', seconds: 2}]};
    throw failure('UNKNOWN_ACTION', '动作不在声明式白名单中');
  }

  function createGuidedModel(initial) {
    const model = createModel(initial);
    model.advancedOpen = false;
    model.compiledMode = null;
    model.replace = function (next) {
      if (!['guided-1.0', 'guided-1.1'].includes(next?.schema_version) || !Array.isArray(next.columns) || !Array.isArray(next.electrodes)) throw failure('DOCUMENT_INVALID', '请使用 guided-1.0 / guided-1.1 模板文档');
      this.document = copy(next); this.invalidate();
    };
    model.selectedColumns = function () { return this.document.columns.filter(column => column.enabled).sort((left, right) => left.index - right.index); };
    model.applyExecution = function (mode, count, routeText) {
      if (!['once', 'repeat', 'custom'].includes(mode)) throw failure('EXECUTION_INVALID', '请选择有限实验模式');
      if (mode === 'repeat' && (!Number.isInteger(count) || count < 1 || count > 500)) throw failure('EXECUTION_INVALID', '重复次数须为 1–500 的整数');
      const route = mode === 'custom' ? parseCustomRoute(routeText) : null;
      this.edit(document => {
        document.schema_version = 'guided-1.1';
        document.execution = {mode, count: mode === 'repeat' ? count : 1, route};
        if (route) for (const column of document.columns) column.enabled = route.includes(column.index);
      });
    };
    model.selectedElectrodes = function () { return this.document.electrodes.filter(electrode => electrode.enabled); };
    model.columnDuration = function (index) {
      const column = this.document.columns.find(item => item.index === index);
      if (!column) throw failure('COLUMN_INVALID', '物理列号须为 1–12');
      return column.duration_s ?? this.document.defaults.dwell_s;
    };
    model.effectiveWindow = function (index, channel) {
      const column = this.document.columns.find(item => item.index === index);
      return copy(column?.windows?.[channel] || {start_s: 0, duration_s: this.columnDuration(index)});
    };
    model.updateColumn = function (index, values) {
      const current = this.document.columns.find(item => item.index === index);
      if (!current) throw failure('COLUMN_INVALID', '物理列号须为 1–12');
      const next = {...copy(current), ...copy(values)}, duration = next.duration_s ?? this.document.defaults.dwell_s;
      if (!Number.isFinite(duration) || duration <= 0) throw failure('DURATION_INVALID', '列时长须大于 0');
      for (const window of Object.values(next.windows || {})) {
        if (!Number.isFinite(window.start_s) || !Number.isFinite(window.duration_s) || window.start_s < 0 || window.duration_s <= 0 || window.start_s + window.duration_s > duration) throw failure('WINDOW_INVALID', '有效采集窗口须完整包含在本列时长内');
      }
      this.edit(document => Object.assign(document.columns.find(item => item.index === index), next));
    };
    model.setColumnEnabled = function (index, enabled) { this.updateColumn(index, {enabled: !!enabled}); };
    model.setColumnDuration = function (index, duration) { this.updateColumn(index, {duration_s: duration}); };
    model.setElectrodeEnabled = function (id, enabled) {
      if (!CHANNELS.includes(id)) throw failure('CHANNEL_INVALID', '电极身份须为 A–H');
      this.edit(document => { document.electrodes.find(item => item.id === id).enabled = !!enabled; });
    };
    model.applySharedSampling = function (settings) {
      const shared = validateSamplingParameters(settings);
      this.edit(document => {
        for (const slot of ['A', 'B']) {
          const board = document.bindings.boards[slot];
          board.settings = {...board.settings, ...shared};
        }
      });
    };
    model.setWindow = function (index, channel, window) {
      if (!CHANNELS.includes(channel)) throw failure('CHANNEL_INVALID', '电极身份须为 A–H');
      const windows = copy(this.document.columns.find(item => item.index === index)?.windows || {});
      if (window === null) delete windows[channel]; else windows[channel] = copy(window);
      this.updateColumn(index, {windows});
    };
    model.acceptPlan = function (plan, requestedDocument, mode) {
      if (requestedDocument !== this.serialize()) throw failure('STALE_PLAN', '预览期间实验已变更，请重新运行');
      if (!plan?.plan_hash) throw failure('PLAN_INVALID', '服务未返回计划指纹');
      this.plan = copy(plan); this.compiledDocument = requestedDocument; this.compiledMode = mode;
    };
    model.runPayload = function (mode, liveConfirmed, key) {
      if (!['simulate', 'live'].includes(mode)) throw failure('MODE_INVALID', '运行模式无效');
      if (!this.plan || this.compiledDocument !== this.serialize() || this.compiledMode !== mode) throw failure('PREVIEW_REQUIRED', '请先预览当前实验与运行模式');
      if (mode === 'live' && !liveConfirmed) throw failure('LIVE_CONFIRMATION_REQUIRED', '请明确确认本次实物运行');
      if (mode === 'live' && !this.document.safety?.motion_policy_confirmed) throw failure('MOTION_POLICY_REQUIRED', '请在高级设置确认运动策略');
      if (mode === 'live' && !this.document.safety?.electrical_policy_confirmed) throw failure('ELECTRICAL_POLICY_REQUIRED', '请在高级设置确认电气策略');
      return {document: copy(this.document), mode, plan_hash: this.plan.plan_hash, idempotency_key: key};
    };
    return model;
  }

  function parseCustomRoute(value) {
    const text = String(value || '').trim();
    const invalid = () => failure('ROUTE_INVALID', '路径格式无效：1–9 可用 13579 简写；10、11、12 单独输入表示该列，多列请用 1,3,10,12，重复列用 1,1；不能使用 0 或空项。');
    if (!text) throw invalid();
    const tokens = text.includes(',') ? text.split(',').map(token => token.trim()) : /^(?:10|11|12)$/.test(text) ? [text] : /^[1-9]+$/.test(text) ? text.split('') : [];
    if (!tokens.length || tokens.some(token => !/^(?:[1-9]|1[0-2])$/.test(token))) throw invalid();
    return tokens.map(Number);
  }

  function routeProgressCaption(snapshot) {
    const progress = snapshot.progress;
    if (!progress) return {percent: null, detail: '暂无本次进度', timing: '等待开始'};
    const percent = Number.isFinite(progress.percent) ? Math.max(0, Math.min(snapshot.state === 'completed' ? 100 : 99.9, progress.percent)) : null;
    const column = progress.route?.[progress.path_index - 1];
    const duration = seconds => Number.isFinite(seconds) ? Math.ceil(seconds) + ' s' : '—';
    const waiting = progress.suspended || ['paused', 'waiting_operator', 'waiting_recovery', 'stopping'].includes(snapshot.state);
    const terminal = ['stopped', 'error'].includes(snapshot.state);
    return {percent, detail: '轮次 ' + (progress.cycle_index ?? '—') + ' / ' + (progress.total_cycles ?? '—') + ' · 路径位置 ' + (progress.path_index ?? '—') + ' / ' + (progress.path_length ?? '—') + ' · 当前物理列 ' + (column ?? '—'),
      timing: '已用 ' + duration(progress.elapsed_s) + ' · ' + (terminal ? '运行已结束，未完成' : waiting ? '等待继续 · 预计剩余时间挂起' : '预计剩余 ' + duration(progress.remaining_s) + (Number.isFinite(progress.estimated_finish_unix_s) ? ' · 预计结束 ' + new Date(progress.estimated_finish_unix_s * 1000).toLocaleTimeString() : ''))};
  }

  async function prepareGuidedRun(model, request, mode, liveConfirmed, key) {
    if (mode === 'live' && !liveConfirmed) throw failure('LIVE_CONFIRMATION_REQUIRED', '请明确确认本次实物运行');
    const original = model.serialize();
    const plan = await request('guided/preview', {document: copy(model.document), mode});
    model.acceptPlan(plan, original, mode);
    return model.runPayload(mode, liveConfirmed, key);
  }

  function createApi(fetcher) {
    return async (path, payload) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetcher(API_ROOT + path, {
          method: payload === undefined ? 'GET' : 'POST',
          headers: {'Content-Type': 'application/json'}, signal: controller.signal,
          ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
        });
        const data = await response.json();
        if (!response.ok) {
          const detail = typeof data.error === 'object' && data.error !== null ? data.error : data;
          throw Object.assign(failure(detail.code || 'REQUEST_FAILED', detail.message || String(data.error || '请求失败'), detail.path || ''), {
            request_id: detail.request_id || data.request_id || '', status: response.status,
          });
        }
        return data;
      } catch (error) {
        if (error.name === 'AbortError') throw failure('REQUEST_TIMEOUT', '请求超时；运行结果可能未确认，请刷新状态后重试');
        throw error;
      } finally { clearTimeout(timer); }
    };
  }

  function positionCaption(snapshot) {
    const format = value => Number.isFinite(value) ? value.toFixed(2) + ' mm' : '未确认';
    return `${snapshot.mode === 'simulate' ? '模拟坐标' : '控制器坐标'} · X ${format(snapshot.position?.X)} · Z ${format(snapshot.position?.Z)}`;
  }

  function channelReadout(channel = {}) {
    const last = value => Array.isArray(value) ? value[value.length - 1] : value;
    const current = channel.latest_current_nA ?? last(channel.current_nA);
    const time = last(channel.time_s);
    return {current: Number.isFinite(current) ? current : null, time: Number.isFinite(time) ? time : null};
  }

  function canConnectMotion(snapshot) {
    return !snapshot.busy || (!!snapshot.recovery_required && !snapshot.motion?.connected);
  }

  function guidedTimingCaption(snapshot) {
    const resting = {paused: '已抬起并暂停', waiting_recovery: '已抬起，等待采集板恢复', completed: '本次实验已完成'};
    if (resting[snapshot.state]) return {clock: resting[snapshot.state], detail: '停在当前列上方的安全高度；不自动返回首列。'};
    const timing = snapshot.column_timing;
    const acquiring = snapshot.state === 'running' && (snapshot.message == null || snapshot.message === 'dwell')
      && (snapshot.column == null || snapshot.column === timing?.column);
    if (acquiring && timing?.started_unix_s != null) {
      const remaining = Number.isFinite(timing.remaining_s) ? timing.remaining_s.toFixed(1) + ' s' : '未确认';
      const elapsed = Number.isFinite(timing.elapsed_s) ? timing.elapsed_s.toFixed(1) : '未确认';
      return {clock: '第 ' + timing.column + ' 列 · 剩余 ' + remaining, detail: '已采集 ' + elapsed + ' / ' + timing.duration_s + ' s · 重连只加入本列剩余时间'};
    }
    return {clock: snapshot.busy ? '等待停稳与 START 成功确认' : '等待开始', detail: '移动与连接准备不计入列时长；未收到 START 确认时不开始倒计时。'};
  }

  function validateJogSettings(speed_mm_s = 0.5, max_speed_mm_s = 1, fast_motion_confirmed = false) {
    if (!Number.isFinite(speed_mm_s) || !Number.isFinite(max_speed_mm_s) || speed_mm_s <= 0
      || max_speed_mm_s <= 0 || max_speed_mm_s > 20 || speed_mm_s > max_speed_mm_s) {
      throw failure('JOG_SPEED_INVALID', '目标速度须大于 0 且不超过本次上限；上限最多 20 mm/s');
    }
    if (max_speed_mm_s > 2 && fast_motion_confirmed !== true) throw failure('JOG_SPEED_INVALID', '快移速度启用前，请先确认电极已离孔、运动方向至少有 10 mm 净空');
    return {speed_mm_s, max_speed_mm_s, ...(max_speed_mm_s > 2 ? {fast_motion_confirmed: true} : {})};
  }

  function createKeyboardAlignmentInput(options) {
    const directions = {a: ['X', 'left'], ArrowLeft: ['X', 'left'], d: ['X', 'right'], ArrowRight: ['X', 'right'],
      w: ['Z', 'up'], ArrowUp: ['Z', 'up'], s: ['Z', 'down'], ArrowDown: ['Z', 'down']};
    const held = new Set(), canceled = new Set(), stopping = new Set();
    const startInterval = options.setInterval || setInterval, endInterval = options.clearInterval || clearInterval;
    const createId = options.createId || (() => scope.crypto?.randomUUID?.() || 'press-' + Date.now() + '-' + Math.random().toString(16).slice(2));
    let active = null, token = null, releaseRequired = false, exhausted = false;
    const context = () => options.context() || {};
    const report = error => options.onError?.(error);
    const current = press => active === press && !canceled.has(press.press_id)
      && context().active === true && context().session_token === press.session_token
      && context().connection_token === press.connection_token;
    const editable = target => ['INPUT', 'TEXTAREA', 'SELECT'].includes(String(target?.tagName || '').toUpperCase())
      || target?.isContentEditable === true || !!target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])');
    const keyName = event => event.key?.length === 1 ? event.key.toLowerCase() : event.key;
    function trace(stage, press = active, details = {}) {
      try { options.onTrace?.({stage, wall_ms: Date.now(), mono_ms: scope.performance?.now?.(),
        session_token: press?.session_token || context().session_token, connection_token: press?.connection_token || context().connection_token,
        press_id: press?.press_id || null, ...details}); } catch (ignoredError) {}
    }
    function stop(reason, requireRelease = false) {
      if (!requireRelease) held.clear();
      releaseRequired = requireRelease && held.size > 0;
      const press = active;
      trace('stop_requested', press, {reason, require_release: requireRelease});
      active = null;
      if (!press) return Promise.allSettled([...stopping]);
      if (press.timer !== null) endInterval(press.timer);
      canceled.add(press.press_id);
      if (canceled.size >= 4096) exhausted = true;
      if (!press.startSent) {
        trace('unsent_press_cancelled', press, {reason});
        options.onStatus?.(reason, requireRelease);
        return Promise.allSettled([...stopping]);
      }
      const stopped = Promise.resolve().then(() => { trace('stop_send', press); return options.request('guided/calibration/jog/stop', {
        session_token: press.session_token, press_id: press.press_id,
      }); }).then(result => {
        trace('stop_response', press, {status: result?.manual?.alignment?.status, confirmed: result?.manual?.alignment?.stop_result?.stop_confirmed});
        const next = context();
        if (next.active === true && next.session_token === press.session_token
          && result?.manual?.session_token === press.session_token) {
          options.onSnapshot?.(result); sync(result);
        }
      }).catch(error => {
        trace('stop_error', press, {status: error.code, reason: error.message});
        if (context().session_token !== press.session_token || context().connection_token !== press.connection_token) return;
        exhausted = true; options.onStatus?.('stop_error', true); report(error);
      }).finally(() => stopping.delete(stopped));
      stopping.add(stopped);
      options.onStatus?.(reason, requireRelease);
      return Promise.allSettled([...stopping]);
    }
    function sync(snapshot) {
      const next = context();
      if (token !== next.session_token) {
        void stop('session');
        token = next.session_token;
        canceled.clear(); exhausted = false;
      }
      const alignment = snapshot?.manual?.alignment;
      if (active && (['frozen', 'blocked', 'closed', 'stopped', 'error'].includes(alignment?.status)
        || (active.acknowledged && alignment?.status === 'stopping')
        || (active.acknowledged && alignment?.status === 'idle' && alignment.last_stop_reason))) void stop('server', true);
      if (active && (!next.active || next.connection_token !== active.connection_token)) void stop('context');
    }
    async function heartbeat(press) {
      if (!current(press)) { if (active === press) void stop('context'); return; }
      if (press.renewing) return;
      press.renewing = true;
      try {
        trace('heartbeat_send', press, {sequence: press.sequence + 1});
        const next = await options.request('guided/calibration/jog/heartbeat', {
          session_token: press.session_token, press_id: press.press_id, sequence: ++press.sequence,
        });
        trace('heartbeat_response', press, {sequence: press.sequence});
        if (current(press)) { options.onSnapshot?.(next); sync(next); }
      } catch (error) { if (active === press) { void stop('heartbeat', true); report(error); } }
      finally { press.renewing = false; }
    }
    function begin(input, direction) {
      sync();
      const next = context();
      if (next.active !== true || typeof next.session_token !== 'string' || !next.session_token) return;
      if (exhausted) { report(failure('JOG_SESSION_RESTART_REQUIRED', '本次按键会话已阻止；请确认停止并取消后，重新开始键盘标定')); return; }
      if (held.has(input)) return;
      held.add(input);
      if (releaseRequired || held.size > 1 || active) { void stop('multiple', true); return; }
      const boundary = (next.boundaries || []).find(item => item.at_limit && item.axis === direction[0] && item.direction === direction[1]);
      if (boundary) {
        held.delete(input);
        trace('direction_limited', null, {axis: direction[0], direction: direction[1]});
        options.onStatus?.('boundary', false);
        return;
      }
      const press = {session_token: next.session_token, connection_token: next.connection_token, press_id: createId(), timer: null, sequence: 0, renewing: false, startSent: false};
      const priorStops = [...stopping];
      active = press;
      trace('press_created', press, {input, axis: direction[0], direction: direction[1], queued_stops: priorStops.length});
      Promise.resolve().then(async () => {
        if (priorStops.length) {
          trace('waiting_for_stop', press);
          await Promise.allSettled(priorStops);
          trace('stop_wait_finished', press, {still_current: current(press)});
          if (!current(press)) return null;
          if (exhausted) { void stop('stop_error', true); return null; }
        }
        if (!current(press)) return null;
        press.startSent = true;
        trace('start_send', press);
        return options.request('guided/calibration/jog/start', {
          session_token: press.session_token, press_id: press.press_id, axis: direction[0], direction: direction[1],
        });
      }).then(result => {
        trace('start_response', press, {still_current: current(press), status: result?.manual?.alignment?.status});
        if (!result || !current(press)) return;
        press.acknowledged = true;
        options.onSnapshot?.(result); sync(result);
        if (current(press)) press.timer = startInterval(() => { void heartbeat(press); }, 100);
      }).catch(error => { if (active === press) { void stop('start', true); report(error); } });
    }
    function release(input) {
      if (!held.has(input)) return;
      held.delete(input);
      if (active) void stop('release', held.size > 0);
      if (held.size === 0) releaseRequired = false;
    }
    return {
      keydown(event) {
        const key = keyName(event);
        if ((directions[key] || key === 'Escape') && !editable(event.target)) trace('keydown', active, {key, repeat: !!event.repeat, dom_time_ms: event.timeStamp});
        if (key === 'Escape') { if (active || context().active) { event.preventDefault?.(); void stop('escape'); } return; }
        if (event.isComposing || event.keyCode === 229 || event.ctrlKey || event.altKey || event.metaKey || editable(event.target)) {
          if (active) void stop('focus');
          return;
        }
        if (!directions[key] || event.repeat) return;
        if (!context().active) return;
        event.preventDefault?.(); begin('key:' + key, directions[key]);
      },
      keyup(event) { const key = keyName(event); if (directions[key]) { if (!editable(event.target)) trace('keyup', active, {key, dom_time_ms: event.timeStamp}); release('key:' + key); } },
      pointerdown(input, direction) { if (directions[direction]) begin('pointer:' + input, directions[direction]); },
      pointerup(input) { release('pointer:' + input); },
      stop, sync, isStopping() { return stopping.size > 0; },
      inputIdle() { return !active && held.size === 0 && stopping.size === 0; },
    };
  }

  const exported = {createModel, createGuidedModel, prepareGuidedRun, guidedTimingCaption, sharedSamplingParameters, boardElectrodeMapping, columnDurationCaption, calibrationReferenceCaption, errorGuidance, actionFor, createApi, positionCaption, channelReadout, canConnectMotion, validateJogSettings, createKeyboardAlignmentInput};
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  if (!scope.document) return;

  const root = document.getElementById('view-throughput');
  if (!root) return;
  const api = createApi(scope.fetch.bind(scope));
  const element = id => root.querySelector('#' + id);
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const movementFields = [
    ['settle_s', '停稳等待', 's'], ['descend_speed_mm_s', '下降速度', 'mm/s'],
    ['ascend_speed_mm_s', '提拉速度', 'mm/s'], ['translate_speed_mm_s', '横移速度', 'mm/s'],
  ];
  const axisFields = ['address', 'lead_mm', 'travel_mm', 'positive_direction', 'max_speed_mm_s'];
  const titles = {move_z: '升降', move_x: '移至孔列', dwell: '停稳后采集', withdraw: '提拉', wait: '等待', wait_operator: '等待人工'};
  const stateNames = {idle: '待机', preparing: '准备连接', running: '实验进行中', stopping: '正在停止', completed: '已完成', error: '需要处理', paused: '已抬起并暂停', waiting_operator: '等待人工继续', waiting_recovery: '已抬起，等待采集板恢复'};
  const phaseNames = {unused: '未使用', configuring: '准备采集', running: '采集中', retrying: '掉线，正在重连', stopped: '已停止', waiting_recovery: '等待恢复'};
  let model = null, scene = null, snapshot = {}, guided = {}, pending = false, polling = false;
  const sectionFeedback = {defaults: {revision: 0, phase: 'idle', message: ''}};
  let jsonDirty = false, documentDirty = false, runKey = null, eventSequence = 0, sceneStarted = false, stateUnavailable = true, requestedMode = 'simulate';
  let limits = {}, limitsUnavailable = true, limitsPolling = false, limitsRevision = 0, limitsAction = false, limitsUncertain = false, limitsReadError = null;
  let twin = {}, twinPolling = false, twinRevision = 0, twinAction = false, twinReadError = null, twinReceivedAt = 0;
  let twinDatumSelected = false, twinDatumSignature = null;
  let calibrationRevision = 0, calibrationConnectionToken = null, stateReceivedAt = 0;
  let calibrationOperation = null;
  let frontendBaseline = null, frontendHealth = null, frontendHealthAt = 0, frontendHealthPolling = false, frontendUpdateRequired = false;
  let devicesCache = scope.PAHighThroughputDevices || null;
  let boards = {}, boardsUnavailable = true, boardsPolling = false, boardsPending = false, boardsRevision = 0;
  let boardDraft = null, boardDraftDirty = false, boardNotice = '', boardNoticeError = false;
  let jsonBoardAddressBase = null;
  const boardAddressEdits = new Set(), boardAddressDrafts = new Set();
  const seenEvents = new Set(), draftAreas = new Set(), normalDrafts = new Set();

  root.innerHTML = [
    '<div class="ht-toolbar panel"><div><span class="ht-eyebrow">高通量自动化实验</span><span id="htFrontendVersion" class="ht-frontend-version"></span><h2>模板实验</h2><p id="htStatus" role="status">正在读取模板…</p></div>',
    '<div class="ht-run-actions"><select id="htMode" aria-label="运行模式"><option value="simulate">模拟实验</option><option value="live">实物实验</option></select><button id="htRun" class="primary" type="button" disabled>开始模拟实验</button><button id="htPause" class="secondary" type="button" disabled>本列结束后暂停</button><button id="htResume" class="secondary" type="button" hidden>继续实验</button><button id="htResetDevices" class="secondary" type="button" title="停止任务、重新连接并清除旧会话；保留数据与日志，不移动电机">重连并重置</button><button id="htStop" class="danger" type="button">停止</button></div></div>',
    '<section id="htFrontendUpdate" class="ht-frontend-update" role="status" hidden><div><strong>当前界面需要刷新</strong><p id="htFrontendUpdateText"></p></div><button id="htFrontendReload" class="secondary" type="button" disabled>确认已松键，刷新界面</button></section>',
    '<section class="panel ht-total-progress"><div class="ht-section-head"><h3>全程进度</h3><strong id="htTotalPercent">—</strong></div><progress id="htTotalProgress" max="100" aria-label="全程实验进度"></progress><p id="htTotalRoute">暂无本次进度</p><p id="htTotalTiming">等待开始</p></section>',
    '<section class="panel ht-execution-panel"><label>实验模式<select id="htExecutionMode"><option value="once">一次性实验</option><option value="repeat">重复实验</option><option value="custom">自定义路径</option></select></label><label id="htExecutionCountLabel" hidden>重复轮数<input id="htExecutionCount" type="number" min="1" max="500" step="1"></label><label id="htExecutionRouteLabel" hidden>列访问顺序<input id="htExecutionRoute" placeholder="13579 或 1,3,10,12"></label><button id="htApplyExecution" class="secondary" type="button">应用执行设置</button><p id="htExecutionStatus" role="status">一次性执行已选列，按列号升序。</p></section>',
    '<div id="htError" class="ht-message ht-error" role="alert" hidden></div>',
    '<details id="htErrorDetails" class="ht-error-details" hidden><summary>技术详情（提供给维护人员）</summary><pre id="htErrorDetailText"></pre></details>',
    '<div id="htSimulationNotice" class="ht-message ht-simulation">模拟实验 · 生成合成数据，不操作实物。3D 仅作流程示意。</div>',
    '<div id="htLiveGate" class="ht-message ht-live-gate" hidden><label class="ht-check"><input id="htLiveConfirmed" type="checkbox">本次允许驱动实物与电极，已确认装夹、工作高度、安全高度及高级设置中的运动与电气策略</label></div>',
    '<section class="panel ht-template-panel"><div class="ht-template-fields"><label>实验模板<select id="htTemplate"><option value="template1">Template 1 · 逐列实验</option></select></label><label>实验名称<input id="htName" aria-label="实验名称"></label><label>模板默认（未覆盖列适用，s）<input id="htDefaultDuration" type="number" min="0.001" step="any" value="180"></label><button id="htSave" class="secondary" type="button">保存模板</button></div><div class="ht-template-summary"><span id="htSelectionSummary">固定物理列 1–12 · 电极 A–H</span><div class="ht-template-tabs" role="tablist" aria-label="模板参数与板卡对应"><button id="htSamplingToggle" type="button" role="tab" aria-selected="false" aria-controls="htSamplingPanel">采样参数（8电极共用）</button><button id="htMappingToggle" type="button" role="tab" aria-selected="false" aria-controls="htMappingPanel">板卡与电极对应</button></div><span id="htEditorState">加载中</span></div>',
    '<section id="htSamplingPanel" class="ht-template-pane" role="tabpanel" aria-labelledby="htSamplingToggle" hidden><p id="htSharedSamplingState" class="ht-sampling-state" role="status">正在读取两板参数…</p><div class="ht-sampling-fields"><label>测试电位 WE−RE (V)<input id="htSharedPotential" type="number" min="-0.4" max="0.4" step="0.01"></label><label>WE 电位 (V)<input id="htSharedWorkingElectrode" type="number" min="0.25" max="1.535" step="0.01"></label><label>硬件采样周期<select id="htSharedPeriod"></select></label><label>电流量程<select id="htSharedFsr"></select></label><label>偏置电流<select id="htSharedOffset"></select></label></div><p id="htSharedPotentialSummary" class="ht-hint"></p><div class="ht-sampling-footer"><p class="ht-hint">当前模板给板 A 的 A–D、板 B 的 E–H 各四路统一设置；板内 DAC / 采样周期共享，量程逐通道配置。此处显式应用相同参数到两板，仅修改模板，实物运行时才下发。</p><button id="htApplySharedSampling" class="secondary" type="button">应用到两块板</button></div></section>',
    '<section id="htMappingPanel" class="ht-template-pane" role="tabpanel" aria-labelledby="htMappingToggle" hidden><div class="ht-editor-actions"><button id="htBoardScan" class="secondary" type="button">扫描蓝牙板卡</button><button id="htBoardBind" class="secondary" type="button">保存 A / B 对应</button><button id="htBoardConnect" class="primary" type="button">连接已绑定板卡</button></div><p id="htBoardNotice" class="ht-hint" role="status">打开面板后读取连接状态</p><div id="htBoardMapping" class="ht-board-mapping"></div><p class="ht-hint">请自行核对实物，将前板指定为 A、后板指定为 B；软件不按扫描顺序、广播名或信号强度判断前后。可将不用的槽位留空。BLE 连接与主界面的单板测量目标独立；软件通道对应仍需核对实际接线。</p></section></section>',
    '<section class="panel ht-calibration-strip"><div><strong>本次上电四点标定</strong><p id="htCalibrationSummary">正在读取标定状态…</p></div><button id="htCalibrationToggle" class="secondary" type="button" aria-expanded="false">标定 / 查看</button></section>',
    '<section id="htCalibrationPanel" class="panel ht-calibration-panel" hidden><div class="ht-section-head"><h3>键盘四点标定 · 第 1 / 12 列与工作 / 安全高度</h3><button id="htCalibrationHide" class="text-button" type="button">收起</button></div><div class="ht-pad"><p id="htCalibrationWarning" class="ht-calibration-warning">保持电机供电，确认电极、孔板与两轴有充足运动余量。进入标定后保持锁轴，用 WASD / 方向键或按住下方箭头对位；松开即请求停止。物理第 1 / 12 列只记录 X，浸入工作 Z 与提出安全 Z 独立记录。</p><p id="htCalibrationAxisStatus" class="ht-calibration-axis-state" role="status">双轴状态未确认</p><label id="htKeyboardConfirmation" class="ht-check"><input id="htCalibrationMotionConfirmed" type="checkbox">已确认电极与孔板姿态、两轴运动余量，允许本次键盘标定运动</label><div id="htLegacyManualConfirmations" hidden><input id="htCalibrationReleaseConfirmed" type="checkbox"><input id="htCalibrationZSupported" type="checkbox"></div><button id="htCalibrationBegin" class="secondary" type="button">进入键盘标定</button><div id="htKeyboardAlignment" class="ht-keyboard-alignment"><div class="ht-jog-settings"><label>目标速度 (mm/s)<input id="htJogSpeed" type="number" min="0.001" max="1" step="0.05" value="0.5"></label><label>本次速度上限 (mm/s)<input id="htJogMaxSpeed" type="number" min="0.001" max="20" step="0.1" value="1"></label><button id="htJogApply" class="secondary" type="button">应用速度</button></div><div class="ht-editor-actions"><button id="htJogFine" class="secondary" type="button">精调 0.5 mm/s</button><button id="htJogFast" class="secondary" type="button">快移 20 mm/s</button><label id="htJogFastClearance" class="ht-check" hidden><input id="htJogFastConfirmed" type="checkbox">已确认电极离孔，运动方向至少有 10 mm 净空</label></div><div class="ht-jog-pad" aria-label="按住对位，松开停止"><button id="htJogUp" type="button" class="secondary" aria-label="按住 Z 上移">↑<span>W · Z 上</span></button><button id="htJogLeft" type="button" class="secondary" aria-label="按住 X 左移">←<span>A · X 左</span></button><button id="htJogStop" type="button" class="danger">停止</button><button id="htJogRight" type="button" class="secondary" aria-label="按住 X 右移">→<span>D · X 右</span></button><button id="htJogDown" type="button" class="secondary" aria-label="按住 Z 下移">↓<span>S · Z 下</span></button></div><p id="htJogStatus" class="ht-jog-status" role="status">进入键盘标定后可按住方向对位</p><p id="htJogFeedback" class="ht-hint">实际位移以编码器反馈为准。</p><p class="ht-hint">速度是指令目标值；短按包含通信、加减速时间，不能用按键时长当实测位移。按住持续移动，松开停止；不再按固定距离或时长中断。快移待执行指令缓冲 ≤6 mm（不是总行程限制），会滚动续接；断连仍可能走完已下发部分。请持续观察剩余行程并提前松键。未标定机械零点，默认 X150 / Z50 行程不作为键盘边界；请目视留足余量。仅明确设置的方向保护、控制器计数范围、活动轴反馈与停止保护有效。运动中另一轴暂停轮询、读数可过期，松开后复核双轴。仅本标定面板与窗口可见、获得焦点时接受方向键；多键同时按下会停止。软件断线停止不能替代现场急停与机械余量。</p></div><div id="htCalibrationSteps" class="ht-calibration-steps"><button id="htCaptureFirst" class="secondary" type="button">读取第 1 列 X</button><button id="htCaptureLast" class="secondary" type="button">读取第 12 列 X</button><button id="htCaptureWorking" class="secondary" type="button">读取浸入工作 Z</button><button id="htCaptureSafe" class="secondary" type="button">读取提出安全 Z</button></div><p id="htCalibrationProgress" role="status"></p><div class="ht-editor-actions"><button id="htCalibrationFinish" class="primary" type="button">完成四点标定</button><button id="htCalibrationCancel" class="secondary" type="button">取消标定</button></div><p class="ht-hint">读取、完成或取消前先停止按键运动。四点只属于本次连接；断电或重连后须重新标定。不会回零、清零或自动返回。控制器旧目标与当前位置不一致时拒绝进入，请停止操作并按提示重新上电，勿再手拧轴。</p></div></section>',
    '<div id="htLegacyCalibration" hidden>',
    '<section class="panel ht-calibration-strip ht-limits-strip"><div><strong>绝对运行限位</strong><p id="htLimitsSummary" role="status">正在读取限位状态…</p></div><button id="htLimitsToggle" class="secondary" type="button" aria-expanded="false" aria-controls="htLimitsPanel">设置 / 查看</button></section>',
    '<section id="htLimitsPanel" class="panel ht-limits-panel" hidden><div class="ht-section-head"><h3>绝对运行限位 · 四个安全边界</h3><button id="htLimitsHide" class="text-button" type="button">收起</button></div><div class="ht-pad"><p class="ht-hint">独立于孔板物理第 1 / 12 列、工作高度与安全高度标定。先主动开始，再按已验证的现场操作将轴对位并停稳，分别点击读取当前位置；按钮只读取编码器，不会移动、松轴、使能或回零。手拧后原位锁定仍须验证，既有标定门禁保持。</p><button id="htLimitsBegin" class="secondary" type="button">开始读取四个边界</button><div id="htLimitBoundaries" class="ht-limit-boundaries"></div><p id="htLimitsProgress" class="ht-hint" role="status"></p><p id="htLimitsSpan" class="ht-limit-span"></p><p id="htLimitsBinding" class="ht-hint"></p><label class="ht-check"><input id="htLimitsClearance" type="checkbox">已留安全余量，未把机械硬挡作为运行边界</label><div class="ht-editor-actions"><button id="htLimitsSave" class="primary" type="button">保存四边界限位</button><button id="htLimitsCancel" class="secondary" type="button">取消读取</button></div><p class="ht-limit-warning">这是软件保护：通信中断、电脑失效、驱动失控或惯性仍可能导致越界。独立保护需要硬限位开关 / 硬件急停；请为停止距离预留余量，不要碰撞机械硬挡找边界。</p><p class="ht-hint">重新连接后，已保存记录仅供查看，须重新读取四点才能用于当前连接。取消不恢复旧运动目标；模拟实验无需实物限位。</p></div></section>',
    '<details id="htTwinDatumPanel" class="panel ht-secondary ht-twin-registration"><summary>3D显示基准 · 名义CAD端点</summary><div class="ht-pad"><p class="ht-hint">手动对位并读取同一连接的两轴边界后，即可建立独立显示基准。只使用服务端已读取的原始计数，不需要填写 CAD 数值。</p><label>已手动对位的边界<select id="htTwinDatumCorner"><option value="left_up">X最左 / Z最高</option><option value="right_down">X最右 / Z最低</option></select></label><label class="ht-check"><input id="htTwinDatumConfirm" type="checkbox">已确认所选两处边界与当前连接的读取记录对应，并接受名义CAD端点的显示假设</label><div class="ht-editor-actions"><button id="htTwinRegisterDatum" class="secondary" type="button" disabled>用已读取的X左/Z上建立3D基准</button><span id="htTwinDatumStatus" class="ht-hint" role="status">请先读取所选的两个边界</span></div><p class="ht-hint">名义 X150 / Z50 模组：左/上对应孔板中心 −75 mm、lift 50 mm，右/下对应 +75 mm、lift 0 mm。安全边界可能与机械端点留有余量，显示采用名义CAD假设，非机械精度验收，不代表安全行程。建立基准不会保存或启用运行限位，不移动、松轴、使能或清零；实物运行仍须完成独立四边界限位与标定。</p></div></details>',
    '<details id="htTwinRegistration" class="panel ht-secondary ht-twin-registration"><summary>高级 · 四边界与CAD坐标精确对应（不额外移动实物）</summary><div class="ht-pad"><p class="ht-hint">可在读取四个安全边界时记录对应的模型坐标，再在保存限位后配准。填写 X 左 / 右边界对应的孔板中心坐标，以及 Z 上 / 下边界对应的模型 lift；这些数值须来自实际装配对应关系，不是运行行程或孔板第 1 / 12 列默认值。无需另摆实物姿态。</p><div class="ht-twin-fields"><label>X 左边界 · 孔板中心 (mm)<input id="htTwinXLeft" type="number" step="any"></label><label>X 右边界 · 孔板中心 (mm)<input id="htTwinXRight" type="number" step="any"></label><label>Z 上边界 · 模型 lift (mm)<input id="htTwinZUp" type="number" step="any"></label><label>Z 下边界 · 模型 lift (mm)<input id="htTwinZDown" type="number" step="any"></label></div><label class="ht-check"><input id="htTwinConfirm" type="checkbox">已确认这四个值是各实测安全边界对应的真实 CAD 坐标</label><div class="ht-editor-actions"><button id="htTwinRegister" class="secondary" type="button" disabled>保存CAD坐标对应</button><span id="htTwinRegisterStatus" class="ht-hint" role="status">请先读取并保存当前连接的四边界限位</span></div><p class="ht-hint">以编码器和实测导程换算，保持 1 mm 对应 1 mm；跨度配准容差 0.1 mm，不缩放模型。CAD 孔板中心参考 49.5 mm、模型参考 lift 35 mm 仅是展示坐标，不是机械零点。保存对应不会改变运动限位，也不移动、使能或清零。</p></div></details>',
    '</div><div class="ht-workspace"><section class="panel ht-scene-panel"><div class="ht-section-head"><div><span class="ht-eyebrow">装置与物理孔位</span><h3>八电极工作台</h3></div><div class="ht-camera-tools"><button class="secondary" id="htCameraAll" type="button">整体</button><button class="secondary" id="htCameraPlate" type="button">孔板</button><button class="secondary" id="htCameraBoards" type="button">双板</button></div></div>',
    '<div id="htStage" class="ht-stage" aria-label="装置3D：拖动旋转，滚轮缩放，点击物理列编辑"><div id="htSceneLoading" class="ht-scene-loading">加载本地 CAD 网格…</div><div id="htTwinStatus" class="ht-twin-status unknown" role="status">3D 未配准 · 实物位置未知 · CAD 参考姿态</div><div id="htSceneLabels" class="ht-scene-labels"></div><div class="ht-scene-legend"><i></i>原始装配 CAD<span>拖动旋转 · 滚轮缩放 · 点选列</span></div></div>',
    '<div class="ht-small-head ht-column-toolbar"><strong>执行哪些物理列</strong><div><button id="htSelectAllColumns" class="text-button" type="button">全选</button><button id="htClearColumns" class="text-button" type="button">清空</button></div></div><div class="ht-plate-strip" id="htColumns" aria-label="物理列勾选与编辑"></div>',
    '<div class="ht-scene-footer"><span id="htPose">CAD 参考姿态 · 非实物位置</span><button id="htAnimate" class="text-button" type="button" disabled>播放流程示意</button></div><p class="ht-caption">X 只移动孔板托盘，Z 只带动悬臂、电极与双板；固定卡座与孔位不随勾选挤位。CAD 参考抬升 35 mm，不代表控制器零点、浸入深度或碰撞验证。</p></section>',
    '<aside class="ht-editor-stack"><section class="panel ht-electrode-panel"><div class="ht-section-head"><h3>使用的电极</h3><span class="ht-pill">固定位置 A–H</span></div><div class="ht-pad"><div id="htAcquire" class="ht-channel-picks"></div><p class="ht-hint">取消勾选将隐藏电极与示意线束，卡座仍留在原位。所有电极共同升降；下方可分别设置有效采集时长。</p></div></section>',
    '<section class="panel ht-column-panel"><div class="ht-section-head"><h3 id="htColumnTitle">物理第 1 列</h3><span class="ht-pill" id="htColumnTag">已选择</span></div><div class="ht-pad"><label>本列名称<input id="htColumnName" placeholder="例如 A / 10微摩尔每升"></label><label><span id="htColumnDurationLabel">本列覆盖时长（留空继承 180 s）</span><input id="htColumnDuration" type="number" min="0.001" step="any" placeholder="继承默认 180"></label><p class="ht-hint">名称为自由文字，允许重复；有数值即覆盖模板时长。按物理列号依次执行，跳过列保留真实间距。</p><div class="ht-small-head"><strong id="htWindowHeading">第 1 列 · 八个电极的有效采集时长 (s)</strong><span>留空覆盖本列</span></div><p id="htWindowScope" class="ht-hint">仅编辑当前列，不会修改其他列。</p><div id="htWindows" class="ht-window-grid"></div><p id="htWindowSummary" class="ht-hint">停稳并收到 START 成功确认后计时。</p></div></section>',
    '<section class="panel ht-progress-panel"><div class="ht-section-head"><h3>实验进度</h3><span id="htProgressMode" class="ht-pill">模拟</span></div><div class="ht-pad"><p id="htClock" class="ht-clock">等待开始</p><p id="htTimingDetail" class="ht-hint">到位前的移动与准备不计入采集时长。</p><div id="htBoardRecovery" class="ht-board-recovery"></div><p class="ht-hint">暂停在本列完成、抬至安全高度后生效。末列完成后停在该列上方，不自动返回。</p></div></section></aside></div>',
    '<section class="panel ht-results-panel"><div class="ht-section-head"><h3>采集结果</h3><span id="htPosition" class="ht-hint">控制器坐标未确认</span></div><div id="htChannels" class="ht-channels"></div><div class="ht-result-footer"><span id="htRunInfo">尚无运行</span><span id="htManifest"></span></div><p class="ht-caption">A–D 属于板 A，E–H 属于板 B；整板掉线影响该板四路。缺失保留为空，不插值、不补零；两板非硬件同步。既有 CH3 模拟异常仍待定位。</p></section>',
    '<details id="htAdvanced" class="panel ht-secondary"><summary>高级设置 · 起始延迟、设备、运动、JSON 与日志</summary><div class="ht-pad"><p class="ht-hint">日常操作直接编辑上方模板。自动实验接近四点端点时低速精靠近，末段不超过 0.5 mm/s；首次安全高度接近也会限速。键盘对位速度不受影响。运动数值为编辑预设，不代表已通过实物碰撞或行程验收。</p>',
    '<section class="ht-advanced-section"><h3>本列起始延迟</h3><p class="ht-hint">各通道窗口须完整包含在本列时长内；超出时请先延长本列。</p><div id="htDelays" class="ht-window-grid"></div></section>',
    '<section class="ht-advanced-section"><h3>运动预设与保存目录</h3><label>输出目录<input id="htOutput" placeholder="服务端默认目录"></label><div id="htDefaults" class="ht-default-grid"></div><div id="htElectrodes" class="ht-electrode-grid"></div><button id="htApplyDefaults" class="secondary" type="button">应用高级参数</button><p id="htDefaultsApplyStatus" class="ht-apply-status" role="status" aria-live="polite"></p></section>',
    '<section class="ht-advanced-section"><h3>设备绑定 / 调试</h3><div class="ht-board-grid" id="htBoards"></div><div class="ht-two"><label>共享 USB-RS485 端口<input id="htPort" list="htPortOptions" placeholder="选择或输入端口"><datalist id="htPortOptions"></datalist></label><label>波特率<input id="htBaud" type="number" value="115200"></label></div><button id="htPorts" class="text-button" type="button">枚举端口</button><div id="htAxes" class="ht-axis-grid"></div><label class="ht-check"><input id="htMotionPolicy" type="checkbox">已确认安全高度、方向、行程、速度与运动策略</label><label class="ht-check"><input id="htElectricalPolicy" type="checkbox">已确认电极极化 / 停止策略与两板电气设置</label><button id="htApplyBindings" class="secondary" type="button">应用设备绑定</button><div class="ht-commission-controls"><button id="htMotionConnect" class="secondary" type="button">连接并读取</button><label class="ht-check"><input id="htReferenceConfirmed" type="checkbox">当前位置已人工确认为参考点</label><button id="htMotionReference" class="secondary" type="button">登记参考点</button><label class="ht-check"><input id="htEnableConfirmed" type="checkbox">允许轴使能</label><button id="htEnableX" class="secondary" type="button">使能 X</button><button id="htEnableZ" class="secondary" type="button">使能 Z</button><button id="htMotionStop" class="danger" type="button">停止双轴</button><button id="htMotionDisconnect" class="secondary" type="button">断开总线</button></div><pre id="htMotionState" class="ht-motion-state">尚未连接</pre></section>',
    '<section class="ht-advanced-section"><div class="ht-section-head"><h3>模板 JSON / Agent API</h3><a href="/api/automation/v1/openapi.json" target="_blank" rel="noopener">API ↗</a></div><textarea id="htJson" spellcheck="false" aria-label="模板 JSON 编辑器"></textarea><div class="ht-editor-actions"><button id="htApplyJson" class="secondary" type="button">应用 JSON</button><button id="htCompile" class="secondary" type="button">预览当前计划</button><span id="htPlanSummary" class="ht-hint">运行时自动预览当前模板</span></div><div id="htTimeline" class="ht-timeline"></div><div id="htWarnings" class="ht-plan-warnings"></div></section>',
    '<section class="ht-advanced-section"><h3>事件与溯源日志</h3><p class="ht-hint">当前指纹为设备、配置、动作与样本的溯源哈希；电化学特征算法待明确。</p><div id="htEvents" class="ht-events" role="log" aria-label="自动化事件日志"></div></section></div></details>',
  ].join('');

  const calibrationErrorDetails = node('details', 'ht-error-details');
  calibrationErrorDetails.id = 'htCalibrationErrorDetails';
  calibrationErrorDetails.hidden = true;
  const calibrationErrorText = node('pre');
  calibrationErrorText.id = 'htCalibrationErrorDetailText';
  calibrationErrorDetails.append(node('summary', '', '标定故障技术详情（提供给维护人员）'), calibrationErrorText);
  element('htCalibrationPanel').append(calibrationErrorDetails);
  const calibrationCompletion = node('p', 'ht-calibration-completion');
  calibrationCompletion.id = 'htCalibrationCompletion'; calibrationCompletion.hidden = true;
  calibrationCompletion.setAttribute('role', 'status');
  element('htCalibrationSteps').append(calibrationCompletion);

  function numberInput(id, title, unit) {
    const wrapper = node('label', '', title + ' (' + unit + ')'), input = node('input');
    input.type = 'number'; input.step = 'any'; input.id = id; wrapper.append(input); return wrapper;
  }
  for (const [boundary, title] of LIMIT_BOUNDARIES) {
    const block = node('div', 'ht-limit-boundary'), button = node('button', 'secondary', '读取 ' + title);
    button.id = 'htLimitCapture-' + boundary; button.type = 'button';
    const status = node('output', '', '未读取'); status.id = 'htLimitValue-' + boundary;
    block.append(button, status); element('htLimitBoundaries').append(block);
  }
  for (let code = 0; code <= 10; code++) {
    const option = node('option', '', 'PERIOD ' + code + (code === 0 ? '（默认）' : '')); option.value = String(code); element('htSharedPeriod').append(option);
  }
  for (const fsr of [50, 100, 250, 500, 1000, 2000]) {
    const option = node('option', '', fsr < 1000 ? fsr + ' nA' : fsr / 1000 + ' µA'); option.value = String(fsr); element('htSharedFsr').append(option);
  }
  for (const offset of SAMPLING_OFFSETS) {
    const option = node('option'); option.value = offset; element('htSharedOffset').append(option);
  }
  for (const [key, title, unit] of movementFields) element('htDefaults').append(numberInput('htDefault-' + key, title, unit));
  for (let index = 1; index <= 12; index++) {
    const cell = node('div', 'ht-column-choice'), label = node('label', 'ht-column-check'), input = node('input');
    input.type = 'checkbox'; input.id = 'htColumnEnabled-' + index; input.setAttribute('aria-label', '执行物理第 ' + index + ' 列');
    input.addEventListener('change', () => editDocument(() => model.setColumnEnabled(index, input.checked), true));
    label.append(input, node('span', '', String(index)));
    const button = node('button', 'ht-column-button'); button.id = 'htColumnEdit-' + index; button.type = 'button'; button.dataset.column = index;
    button.setAttribute('aria-label', '编辑物理第 ' + index + ' 列'); button.addEventListener('click', () => selectColumn(index));
    cell.append(label, button); element('htColumns').append(cell);
  }
  for (const id of CHANNELS) {
    const pick = node('label', 'ht-channel-pick'), checkbox = node('input');
    checkbox.type = 'checkbox'; checkbox.id = 'htElectrodeEnabled-' + id; checkbox.value = id; checkbox.setAttribute('aria-label', '使用电极 ' + id);
    checkbox.addEventListener('change', () => editDocument(() => model.setElectrodeEnabled(id, checkbox.checked), true));
    pick.append(checkbox, node('span', '', id)); element('htAcquire').append(pick);
    const window = numberInput('htWindow-' + id, id, 's'); window.querySelector('input').min = '0.001'; element('htWindows').append(window);
    const delay = numberInput('htDelay-' + id, id + ' 起始延迟', 's'); delay.querySelector('input').min = '0'; element('htDelays').append(delay);
    for (const prefix of ['htWindow-', 'htDelay-']) element(prefix + id).addEventListener('input', () => updateWindow(id, prefix + id));
    const identity = node('label', '', id + ' · 板 ' + (id < 'E' ? 'A' : 'B') + ' CH' + (CHANNELS.indexOf(id) % 4 + 1));
    const name = node('input'); name.id = 'htElectrode-' + id; identity.append(name); element('htElectrodes').append(identity);
    const card = node('article', 'ht-channel-card'); card.id = 'htCard-' + id;
    card.append(node('span', 'ht-channel-id', id), node('strong', 'ht-channel-value', '—'), node('span', 'ht-channel-unit', 'nA'), node('small', 'ht-channel-name', '等待记录'), node('small', 'ht-channel-detail', '尚无样本')); element('htChannels').append(card);
  }
  for (const slot of ['A', 'B']) {
    const mapping = node('article', 'ht-mapping-card');
    mapping.append(node('strong', '', '板 ' + slot + (slot === 'A' ? ' · 前板（远离 Z 轴电机）' : ' · 后板（靠近 Z 轴电机）')));
    const choiceLabel = node('label', '', '已发现设备 / 完整 BLE UUID'), choice = node('select');
    choice.id = 'htBoardSelect-' + slot; choiceLabel.append(choice);
    const identity = node('code'); identity.id = 'htBoardIdentity-' + slot;
    const connection = node('p', 'ht-board-connection'); connection.id = 'htBoardConnection-' + slot; connection.setAttribute('role', 'status');
    const disconnect = node('button', 'secondary', '断开板 ' + slot); disconnect.id = 'htBoardDisconnect-' + slot; disconnect.type = 'button';
    mapping.append(choiceLabel, identity, connection, node('p', '', CHANNELS.slice(slot === 'A' ? 0 : 4, slot === 'A' ? 4 : 8).map((electrode, index) => electrode + ' → CH' + (index + 1)).join(' · ')), disconnect);
    element('htBoardMapping').append(mapping);
    const block = node('div'); block.append(node('h4', '', '板 ' + slot + ' · ' + (slot === 'A' ? 'A–D' : 'E–H')));
    const addressLabel = node('label', '', 'BLE 身份 / 地址'), address = node('input'); address.id = 'htBoardAddress-' + slot; addressLabel.append(address);
    const settingsLabel = node('label', '', '本板采集设置 JSON'), settings = node('textarea'); settings.id = 'htBoardSettings-' + slot; settings.spellcheck = false; settingsLabel.append(settings);
    block.append(addressLabel, settingsLabel); element('htBoards').append(block);
  }
  for (const axis of ['X', 'Z']) {
    const block = node('div', 'ht-axis-block'); block.append(node('h4', '', axis + ' 轴'));
    for (const [key, title, unit] of [['address', '地址', '1–255'], ['lead_mm', '导程', 'mm'], ['travel_mm', '行程', 'mm'], ['positive_direction', '正向', '0 / 1'], ['max_speed_mm_s', '速度上限', 'mm/s']]) block.append(numberInput('htAxis-' + axis + '-' + key, title, unit));
    element('htAxes').append(block);
  }

  function showError(error) {
    element('htError').textContent = errorGuidance(error, guided.manual, snapshot);
    element('htError').hidden = false;
    element('htErrorDetailText').textContent = [error.code, error.message || String(error), error.path ? '字段：' + error.path : '', error.request_id ? '请求：' + error.request_id : ''].filter(Boolean).join('\n');
    element('htErrorDetails').hidden = false;
    element('htErrorDetails').open = false;
  }
  async function handle(action) {
    if (pending) return;
    pending = true; element('htError').hidden = true; element('htErrorDetails').hidden = true; updateControls();
    try { await action(); } catch (error) { showError(error); }
    finally { pending = false; updateControls(); }
  }
  function requireDocument(allowedArea) {
    if (allowedArea !== 'calibration' && (snapshot.busy || guided.manual?.active || boardsPending)) throw failure('BUSY', '实验或标定进行中，不能修改或保存实验');
    if (limitCalibrationActive()) throw failure('LIMITS_CALIBRATING', '四边界限位读取期间，实验与普通轴操作已暂停');
    if (!model) throw failure('DOCUMENT_LOADING', '模板尚未读入');
    if (jsonDirty && allowedArea !== 'json') throw failure('JSON_NOT_APPLIED', '请先应用或还原高级 JSON 草稿');
    if ((!allowedArea || allowedArea === 'calibration') && draftAreas.size) throw failure('FIELDS_NOT_APPLIED', '请先应用已编辑的采样参数、高级参数或设备绑定');
    if ((!allowedArea || allowedArea === 'calibration') && normalDrafts.size) throw failure('FIELDS_INVALID', '请先修正正在编辑的时长或采集窗口');
  }
  function limitCalibrationActive() { return limits.calibrating === true || limitsUncertain; }
  function limitsActive() { return !limitsUnavailable && limits.state === 'active' && limits.active === true && !limits.error && !limitCalibrationActive(); }
  function limitsComplete() { return LIMIT_BOUNDARIES.every(([boundary]) => Number.isFinite(limits.draft?.captures?.[boundary]?.raw_counts?.[boundary.startsWith('x_') ? 'X' : 'Z'])); }
  function calibrationMotion() {
    if (stateUnavailable) return null;
    const receivedAt = devicesCache ? devicesCache.received_at_ms : stateReceivedAt;
    if ((devicesCache && devicesCache.available !== true) || !Number.isFinite(receivedAt) || receivedAt <= 0) return null;
    const elapsed = Math.max(0, (Date.now() - receivedAt) / 1000);
    const motion = devicesCache ? devicesCache.snapshot?.motion : snapshot.motion;
    if (!motion?.connected || motion.faulted || motion.feedback_error || elapsed > 2) return null;
    return {motion, elapsed};
  }
  function calibrationAxisState(axis) {
    const context = calibrationMotion(), feedback = context?.motion.axes?.[axis];
    if (!feedback || feedback.telemetry_valid !== true || typeof feedback.enabled !== 'boolean'
      || !Number.isFinite(feedback.speed_rpm)
      || (devicesCache && !Number.isFinite(feedback.feedback_age_s))
      || (Number.isFinite(feedback.feedback_age_s) && (feedback.feedback_age_s < 0 || feedback.feedback_age_s + context.elapsed > 2))
      || (Number.isInteger(feedback.status) && (feedback.status & 0x3c))) return null;
    return feedback;
  }
  function fourPointCalibrationReady() {
    const calibration = guided.calibration, context = calibrationMotion();
    return guided.four_point_required === true && calibration?.usable === true && calibration.four_point === true
      && !guided.manual?.active && CALIBRATION_POINTS.every(([point]) => calibration.points?.[point])
      && typeof calibration.connection_token === 'string' && !!calibration.connection_token
      && calibration.connection_token === context?.motion.connection_token
      && (!devicesCache || (devicesCache.available === true && devicesCache.snapshot?.calibration?.ready === true))
      && ['X', 'Z'].every(axis => { const feedback = calibrationAxisState(axis); return feedback?.enabled === true && feedback.speed_rpm === 0; });
  }
  function liveCalibrationReady() { return guided.four_point_required === true ? fourPointCalibrationReady() : limitsActive(); }
  function calibrationPointsComplete() { return CALIBRATION_POINTS.every(([point]) => guided.manual?.points?.[point]); }
  const keyboardMode = () => guided.manual?.control_mode === 'keyboard';
  function keyboardControllerAvailable() {
    return !stateUnavailable && snapshot.stop_confirmed === true && !snapshot.recovery_required
      && ['idle', 'completed', 'stopped'].includes(snapshot.state) && !limitCalibrationActive()
      && (!snapshot.busy || (keyboardMode() && guided.manual?.active === true));
  }
  function keyboardRestartAvailable() {
    return keyboardMode() && guided.manual?.active === true && ['blocked', 'released'].includes(guided.manual.status)
      && keyboardControllerAvailable();
  }
  function keyboardConnectionAvailable() {
    return snapshot.motion?.connected === true || (keyboardMode() && typeof model?.document.bindings?.motion?.port === 'string'
      && model.document.bindings.motion.port.trim() !== '');
  }
  function keyboardSessionOwned(allowStopping = false) {
    const manual = guided.manual || {}, alignment = manual.alignment || {};
    const motion = calibrationMotion()?.motion, driver = motion?.manual;
    const pollingOwned = alignment.status === 'holding' && ['X', 'Z'].includes(alignment.axis)
      && driver?.active === true && driver.control_mode === 'keyboard'
      && driver.session_token === manual.session_token && driver.connection_token === motion.connection_token
      && driver.polling_axis === alignment.axis;
    const axesOwned = ['X', 'Z'].every(axis => {
      if (calibrationAxisState(axis)?.enabled === true) return true;
      const held = motion?.axes?.[axis];
      return pollingOwned && axis !== alignment.axis && held?.enabled === true && held.speed_rpm === 0
        && Number.isInteger(held.status) && !(held.status & 0x3c)
        && Number.isFinite(held.feedback_age_s) && held.feedback_age_s + (calibrationMotion()?.elapsed || 0) > 2;
    });
    return keyboardMode() && manual.active === true && !keyboardSessionStopped && typeof manual.session_token === 'string' && !!manual.session_token
      && manual.session_token === alignment.session_token && !['blocked', 'error'].includes(manual.status)
      && (['idle', 'holding'].includes(alignment.status) || allowStopping && alignment.status === 'stopping')
      && axesOwned;
  }
  let jogSettingsDirty = false, jogNotice = '', jogOrigin = null, keyboardSessionStopped = false;
  function keyboardContext() {
    const manual = guided.manual || {}, alignment = manual.alignment || {};
    const waitingForOwnStop = keyboardInput.isStopping();
    return {session_token: manual.session_token, boundaries: alignment.boundaries || [],
      connection_token: (devicesCache ? devicesCache.snapshot?.motion : snapshot.motion)?.connection_token,
      active: keyboardSessionOwned(waitingForOwnStop)
      && typeof manual.session_token === 'string' && !!manual.session_token && !pending && keyboardControllerAvailable() && !keyboardSessionStopped
      && !stateUnavailable && snapshot.motion?.connected === true && !snapshot.motion?.faulted && !snapshot.recovery_required
      && !['blocked', 'error'].includes(manual.status) && !['frozen', 'blocked', 'closed', 'error'].includes(alignment.status)
      && (alignment.status !== 'stopping' || waitingForOwnStop)
      && root.classList.contains('active') && !root.hidden && !element('htCalibrationPanel').hidden
      && document.hidden !== true && document.visibilityState !== 'hidden' && document.hasFocus?.() === true};
  }
  async function keyboardRequest(route, payload) {
    const connectionToken = (devicesCache ? devicesCache.snapshot?.motion : snapshot.motion)?.connection_token;
    const sameSession = () => guided.manual?.session_token === payload.session_token
      && typeof connectionToken === 'string' && !!connectionToken
      && snapshot.motion?.connected === true && snapshot.motion.connection_token === connectionToken
      && (!devicesCache || devicesCache.available === true && devicesCache.snapshot?.motion?.connection_token === connectionToken);
    const result = await api(route, payload);
    if (route !== 'guided/calibration/jog/stop') return result;
    if (!sameSession()) return null;
    const manual = result?.manual, alignment = manual?.alignment;
    if (manual?.session_token === payload.session_token && alignment?.session_token === payload.session_token
      && alignment.status === 'blocked' && alignment.stop_result?.stop_confirmed === true) {
      const cause = alignment.error || manual.error || {};
      throw Object.assign(failure('JOG_STOPPED_FAULT', '双轴已确认停止；原故障：' + (cause.message || cause.code || '会话已中断')),
        {cause, wire: cause.wire});
    }
    if (manual?.session_token !== payload.session_token || alignment?.session_token !== payload.session_token
      || alignment.status !== 'idle' || alignment.stop_result?.stop_confirmed !== true) {
      throw failure('JOG_STOP_UNCONFIRMED', '按键停止尚未确认，请先确认双轴停止');
    }
    const receivedAt = Date.now(), next = await api('devices');
    if (!sameSession()) return null;
    const motion = next?.motion, driver = motion?.manual;
    const elapsed = Math.max(0, (Date.now() - receivedAt) / 1000);
    if (motion?.connected !== true || motion.faulted || motion.feedback_error || motion.connection_token !== connectionToken
      || driver?.active !== true || driver.control_mode !== 'keyboard' || driver.session_token !== payload.session_token
      || driver.connection_token !== connectionToken || !['X', 'Z'].every(axis => {
        const feedback = motion.axes?.[axis];
        return feedback?.telemetry_valid === true && feedback.enabled === true && feedback.speed_rpm === 0
          && Number.isFinite(feedback.feedback_age_s) && feedback.feedback_age_s >= 0 && feedback.feedback_age_s + elapsed <= 2
          && !(Number.isInteger(feedback.status) && (feedback.status & 0x3c));
      })) throw failure('JOG_STOP_FEEDBACK_INVALID', '停止后的双轴状态未确认，请先确认双轴停止');
    devicesCache = {snapshot: next, available: true, error: null, received_at_ms: receivedAt};
    scope.PAHighThroughputDevices = devicesCache;
    return result;
  }
  const keyboardTraceQueue = [];
  const keyboardTracePage = scope.crypto?.randomUUID?.() || String(Date.now());
  let keyboardTraceSequence = 0, keyboardTraceDropped = 0, keyboardTracePending = false;
  function enqueueKeyboardTrace(event) {
    if (keyboardTraceQueue.length >= 128) { keyboardTraceQueue.shift(); keyboardTraceDropped++; }
    keyboardTraceQueue.push({page_id: keyboardTracePage, trace_sequence: ++keyboardTraceSequence, dropped_total: keyboardTraceDropped, ...event});
  }
  setInterval(() => {
    if (keyboardTracePending || !keyboardTraceQueue.length) return;
    const events = keyboardTraceQueue.splice(0, 32);
    keyboardTracePending = true;
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 1500);
    scope.fetch('/api/diagnostics/client', {method: 'POST', headers: {'Content-Type': 'application/json'}, signal: controller.signal,
      body: JSON.stringify({kind: 'keyboard_trace', context: {events}})})
      .then(response => { if (!response.ok) throw new Error('Keyboard trace upload failed'); return response.json(); })
      .catch(() => { keyboardTraceDropped += events.length; }).finally(() => { clearTimeout(timeout); keyboardTracePending = false; });
  }, 100);
  const keyboardInput = createKeyboardAlignmentInput({
    onTrace: enqueueKeyboardTrace,
    request: keyboardRequest, context: keyboardContext,
    onSnapshot(next) { calibrationRevision++; guided = next; renderCalibration(); },
    onError(error) { showError(error); },
    onStatus(reason, requiresRelease) {
      if (reason === 'stop_error') { keyboardSessionStopped = true; updateControls(); }
      jogNotice = reason === 'boundary' ? '该方向受到已报告的方向保护限制，请使用反方向退出；这不是机械终点检测'
        : reason === 'multiple' ? '多方向同时按下：已请求停止，请松开所有方向键后重按'
        : requiresRelease ? '已请求停止，请松开所有方向键后重按' : '已请求停止';
      element('htJogStatus').textContent = jogNotice;
    },
  });
  element('htJogSpeed').value = '0.5'; element('htJogMaxSpeed').value = '1';
  element('htFrontendVersion').textContent = '界面 ' + FRONTEND_BUILD;
  function frontendReloadAllowed() {
    const now = Date.now(), alignment = guided.manual?.alignment || {};
    const idleCalibration = guided.manual?.active === true && snapshot.state === 'idle'
      && !['holding', 'stopping'].includes(alignment.status) && alignment.active !== true;
    return frontendUpdateRequired && !!frontendHealth && now - frontendHealthAt <= 7500
      && !stateUnavailable && stateReceivedAt > 0 && now - stateReceivedAt <= 2500
      && !pending && !boardsPending && keyboardInput.inputIdle()
      && snapshot.stop_confirmed === true && !['running', 'preparing', 'stopping', 'paused', 'waiting_recovery', 'waiting_operator'].includes(snapshot.state)
      && alignment.stop_result?.stop_confirmed !== false && snapshot.motion?.stop_confirmed !== false
      && (!snapshot.busy || idleCalibration) && (frontendHealth.hardware_busy === false || idleCalibration)
      && !['holding', 'stopping'].includes(alignment.status) && alignment.active !== true
      && ['X', 'Z'].every(axis => !Number.isFinite(snapshot.motion?.axes?.[axis]?.speed_rpm) || snapshot.motion.axes[axis].speed_rpm === 0);
  }
  function renderFrontendUpdate() {
    element('htFrontendUpdate').hidden = !frontendUpdateRequired;
    element('htFrontendReload').disabled = !frontendReloadAllowed();
    element('htFrontendUpdateText').textContent = '后台已更新，请松开所有方向键后刷新此界面。刷新不会清空后台采点；旧连接失效时仍需复核。'
      + (frontendReloadAllowed() ? '' : ' 等待确认停止且当前操作结束后才能刷新。');
  }
  async function refreshFrontendVersion() {
    if (frontendHealthPolling || document.hidden || document.visibilityState === 'hidden') return;
    frontendHealthPolling = true;
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await scope.fetch('/api/health', {method: 'GET', cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('Backend health unavailable');
      const next = await response.json();
      if (typeof next.diagnostic_session !== 'string' || !next.diagnostic_session) throw new Error('Backend identity unavailable');
      if (frontendBaseline) {
        if ((frontendBaseline.diagnostic_session && next.diagnostic_session && frontendBaseline.diagnostic_session !== next.diagnostic_session)
          || (frontendBaseline.frontend_revision && next.frontend_revision && frontendBaseline.frontend_revision !== next.frontend_revision)) frontendUpdateRequired = true;
      } else {
        frontendBaseline = {diagnostic_session: next.diagnostic_session, frontend_revision: next.frontend_revision};
        element('htFrontendVersion').title = '界面标识：' + FRONTEND_BUILD + (next.frontend_revision ? ' · 首次连接资源指纹：' + next.frontend_revision : '');
      }
      frontendHealth = next; frontendHealthAt = Date.now();
    } catch (error) { frontendHealth = null; }
    finally { clearTimeout(timeout); frontendHealthPolling = false; renderFrontendUpdate(); }
  }
  element('htFrontendReload').addEventListener('click', () => {
    renderFrontendUpdate();
    if (frontendReloadAllowed()) scope.location?.reload();
  });
  function updateControls() {
    element('htResetDevices').disabled = pending;
    const busy = !!snapshot.busy, manual = !!guided.manual?.active, readingLimits = limitCalibrationActive(), blocked = !model || pending || !!boardsPending || busy || manual || readingLimits;
    element('htRun').disabled = blocked || jsonDirty || draftAreas.size > 0 || normalDrafts.size > 0 || stateUnavailable || (requestedMode === 'live' && (!element('htLiveConfirmed').checked || !liveCalibrationReady()));
    element('htRun').textContent = requestedMode === 'simulate' ? '开始模拟实验' : '开始实物实验';
    element('htMode').disabled = blocked;
    element('htLiveConfirmed').disabled = pending || busy || manual || readingLimits;
    element('htPause').disabled = !busy || pending || readingLimits || snapshot.pause_requested || ['paused', 'waiting_recovery', 'waiting_operator', 'stopping'].includes(snapshot.state);
    element('htPause').textContent = snapshot.pause_requested ? '已请求 · 本列抬起后暂停' : '本列结束后暂停';
    element('htResume').hidden = !['paused', 'waiting_recovery', 'waiting_operator'].includes(snapshot.state);
    element('htResume').disabled = pending || readingLimits;
    element('htLiveGate').hidden = requestedMode !== 'live'; element('htSimulationNotice').hidden = requestedMode !== 'simulate';
    for (const id of ['htSave', 'htApplyDefaults', 'htApplyBindings', 'htApplyJson', 'htCompile', 'htApplySharedSampling']) element(id).disabled = blocked;
    for (const id of ['htExecutionMode', 'htExecutionCount', 'htExecutionRoute', 'htApplyExecution']) element(id).disabled = blocked;
    element('htAnimate').disabled = !model?.plan || jsonDirty || busy || readingLimits || physicalAttached();
    for (const id of ['htMotionReference', 'htEnableX', 'htEnableZ', 'htMotionDisconnect']) element(id).disabled = blocked;
    element('htMotionConnect').disabled = !model || pending || manual || readingLimits || !canConnectMotion(snapshot);
    const calibrationConfirmed = keyboardMode() ? element('htCalibrationMotionConfirmed').checked
      : element('htCalibrationReleaseConfirmed').checked && element('htCalibrationZSupported').checked;
    const restart = keyboardRestartAvailable();
    const calibrationBusy = busy && !(keyboardMode() && manual && keyboardControllerAvailable());
    element('htCalibrationBegin').disabled = !model || pending || calibrationBusy || readingLimits || (manual && !restart)
      || stateUnavailable || !keyboardConnectionAvailable() || !calibrationConfirmed || (keyboardMode() && snapshot.recovery_required === true);
    element('htCalibrationMotionConfirmed').disabled = pending || calibrationBusy || (manual && !restart) || readingLimits;
    for (const id of ['htCalibrationReleaseConfirmed', 'htCalibrationZSupported']) element(id).disabled = pending || busy || manual || readingLimits;
    for (const id of ['htCaptureFirst', 'htCaptureLast', 'htCaptureWorking', 'htCaptureSafe', 'htCalibrationFinish']) element(id).disabled = pending || readingLimits || !manual || (keyboardMode() && (!keyboardControllerAvailable() || !keyboardSessionOwned()));
    element('htCalibrationCancel').disabled = pending || readingLimits || !manual;
    element('htCalibrationFinish').disabled ||= !calibrationPointsComplete();
    for (const [id, axis, direction] of [['htJogUp', 'Z', 'up'], ['htJogDown', 'Z', 'down'], ['htJogLeft', 'X', 'left'], ['htJogRight', 'X', 'right']]) {
      const limited = (guided.manual?.alignment?.boundaries || []).some(boundary => boundary.at_limit && boundary.axis === axis && boundary.direction === direction);
      element(id).disabled = !keyboardContext().active || jogSettingsDirty || limited;
      element(id).title = limited ? '该方向受到已报告的方向保护限制；反方向退出仍可用，不代表机械终点' : '';
    }
    for (const id of ['htJogSpeed', 'htJogMaxSpeed', 'htJogApply', 'htJogFine', 'htJogFast', 'htJogFastConfirmed']) element(id).disabled = pending || !keyboardControllerAvailable() || !keyboardSessionOwned();
    keyboardInput.sync(guided);
    for (const input of root.querySelectorAll('.ht-template-panel input,.ht-template-panel select,.ht-column-panel input,.ht-electrode-panel input,.ht-column-choice input,.ht-advanced-section input,.ht-advanced-section textarea')) input.disabled = blocked;
    element('htJson').disabled = blocked;
    element('htSelectAllColumns').disabled = blocked; element('htClearColumns').disabled = blocked;
    element('htLimitsBegin').disabled = pending || stateUnavailable || limitsUnavailable || busy || manual || readingLimits || !snapshot.motion?.connected;
    for (const [boundary] of LIMIT_BOUNDARIES) element('htLimitCapture-' + boundary).disabled = pending || limitsUnavailable || !limits.calibrating;
    element('htLimitsClearance').disabled = pending || limitsUnavailable || !limits.calibrating;
    element('htLimitsSave').disabled = pending || limitsUnavailable || !limits.calibrating || !limitsComplete() || !element('htLimitsClearance').checked;
    element('htLimitsCancel').disabled = pending || !readingLimits;
    for (const id of ['htTwinXLeft', 'htTwinXRight', 'htTwinZUp', 'htTwinZDown', 'htTwinConfirm']) element(id).disabled = pending || busy || manual;
    element('htTwinRegister').disabled = pending || busy || manual || readingLimits || !limitsActive() || !element('htTwinConfirm').checked || !twinFieldsComplete();
    element('htTwinDatumCorner').disabled = pending || !twinDatumInteractionAllowed();
    element('htTwinDatumConfirm').disabled = pending || !canRegisterTwinDatum();
    element('htTwinRegisterDatum').disabled = pending || !canRegisterTwinDatum() || !element('htTwinDatumConfirm').checked;
    updateBoardControls();
    renderFrontendUpdate();
  }
  function documentChanged(render = false) {
    documentDirty = true;
    runKey = null; model.invalidate(); element('htLiveConfirmed').checked = false;
    element('htJson').value = JSON.stringify(model.document, null, 2); jsonDirty = false;
    element('htEditorState').textContent = '模板已修改 · 尚未保存';
    element('htPlanSummary').textContent = '运行时自动预览最新模板'; element('htTimeline').replaceChildren(); element('htWarnings').replaceChildren();
    scene?.stopPreview(); syncScene();
    if (render) renderColumn();
    renderSelection(); renderSampling(); updateControls();
  }
  function editDocument(change, render = false) {
    try { requireDocument(); change(); documentChanged(render); }
    catch (error) { showError(error); if (model) renderColumn(); }
  }
  function syncScene() {
    if (!model || !scene) return;
    scene.setColumns(model.document.columns); scene.setElectrodes(model.document.electrodes); scene.selectColumn(model.selectedColumn);
  }
  function selectColumn(index) {
    if (!model) return;
    if (normalDrafts.size) { showError(failure('FIELDS_INVALID', '请先修正当前列的时长或采集窗口')); return; }
    model.selectColumn(index); renderColumn(); scene?.selectColumn(index);
  }
  function renderColumnHints() {
    const column = model.document.columns.find(item => item.index === model.selectedColumn), duration = model.columnDuration(column.index);
    const caption = columnDurationCaption(model.document, column.index);
    element('htColumnDurationLabel').textContent = caption.label;
    element('htWindowHeading').textContent = '第 ' + column.index + ' 列 · 八个电极的有效采集时长 (s)';
    element('htWindowScope').textContent = '下面 A–H 仅属于第 ' + column.index + ' 列，不会修改其他列；各列时长见左侧列按钮。';
    element('htColumnDuration').placeholder = '继承默认 ' + model.document.defaults.dwell_s;
    for (const id of CHANNELS) element('htWindow-' + id).placeholder = String(duration);
    const customized = Object.keys(column.windows || {}).length;
    element('htWindowSummary').textContent = caption.summary + ' · ' + (customized ? customized + ' 路自定义窗口；起始延迟见高级设置。' : '各电极默认覆盖本列；起始延迟见高级设置。');
  }
  function renderSelection() {
    if (!model) return;
    element('htSelectionSummary').textContent = '已选 ' + model.selectedColumns().length + '/12 列 · ' + model.selectedElectrodes().length + '/8 电极 · 按物理列号执行';
    for (const column of model.document.columns) {
      element('htColumnEnabled-' + column.index).checked = column.enabled;
      const button = element('htColumnEdit-' + column.index), duration = model.columnDuration(column.index);
      button.textContent = (column.name || '未命名') + '\n' + duration + ' s';
      button.title = '物理第 ' + column.index + ' 列 · ' + (column.name || '未命名') + ' · 本列实际 ' + duration + ' s';
      button.setAttribute('aria-label', '编辑物理第 ' + column.index + ' 列 · 本列实际 ' + duration + ' s');
      button.classList.toggle('active', column.index === model.selectedColumn); button.classList.toggle('unused', !column.enabled); button.setAttribute('aria-pressed', String(column.index === model.selectedColumn));
    }
    for (const electrode of model.document.electrodes) element('htElectrodeEnabled-' + electrode.id).checked = electrode.enabled;
  }
  function renderColumn() {
    if (!model) return;
    const column = model.document.columns.find(item => item.index === model.selectedColumn), duration = model.columnDuration(column.index);
    element('htColumnTitle').textContent = '物理第 ' + column.index + ' 列';
    element('htColumnTag').textContent = column.enabled ? '执行此列' : '跳过此列';
    element('htColumnName').value = column.name; element('htColumnDuration').value = column.duration_s ?? '';
    element('htColumnDuration').placeholder = '继承默认 ' + model.document.defaults.dwell_s;
    for (const id of CHANNELS) {
      element('htWindow-' + id).value = column.windows?.[id]?.duration_s ?? '';
      element('htWindow-' + id).placeholder = String(duration);
      element('htDelay-' + id).value = column.windows?.[id]?.start_s ?? '';
      element('htDelay-' + id).placeholder = '0';
    }
    renderColumnHints();
    renderSelection();
  }
  function renderDocument() {
    const document = model.document;
    if (!draftAreas.has('execution')) {
      const execution = document.execution || {mode: 'once', count: 1, route: null};
      element('htExecutionMode').value = execution.mode;
      element('htExecutionCount').value = execution.count;
      element('htExecutionRoute').value = execution.route?.join(',') || '';
      element('htExecutionStatus').textContent = executionSummary(documentDirty ? '当前模板待保存' : '执行设置已保存');
      element('htApplyExecution').textContent = '应用执行设置';
      element('htApplyExecution').className = 'secondary';
      renderExecutionInputs();
    }
    element('htName').value = document.name || ''; element('htOutput').value = document.output_dir || ''; element('htDefaultDuration').value = document.defaults.dwell_s;
    for (const [key] of movementFields) element('htDefault-' + key).value = document.defaults?.[key] ?? '';
    for (const id of CHANNELS) element('htElectrode-' + id).value = document.electrodes.find(item => item.id === id)?.name || id;
    for (const slot of ['A', 'B']) {
      element('htBoardAddress-' + slot).value = document.bindings?.boards?.[slot]?.address || '';
      element('htBoardSettings-' + slot).value = JSON.stringify(document.bindings?.boards?.[slot]?.settings || {}, null, 2);
    }
    const motion = document.bindings?.motion || {};
    element('htPort').value = motion.port || ''; element('htBaud').value = motion.baud || 115200;
    for (const axis of ['X', 'Z']) for (const key of axisFields) element('htAxis-' + axis + '-' + key).value = motion.axes?.[axis]?.[key] ?? '';
    element('htMotionPolicy').checked = !!document.safety?.motion_policy_confirmed; element('htElectricalPolicy').checked = !!document.safety?.electrical_policy_confirmed;
    element('htJson').value = JSON.stringify(document, null, 2); renderColumn(); renderSampling(); syncScene();
  }
  function renderSamplingPotential() {
    const potentialText = element('htSharedPotential').value.trim(), workingText = element('htSharedWorkingElectrode').value.trim();
    const potential = Number(potentialText), working = Number(workingText), reference = working - potential;
    element('htSharedPotentialSummary').textContent = potentialText && workingText && Number.isFinite(potential) && Number.isFinite(working)
      ? '请求 WE−RE = ' + (potential >= 0 ? '+' : '') + potential.toFixed(3) + ' V；WE = ' + working.toFixed(3) + ' V，推算 RE = ' + reference.toFixed(3) + ' V。实际 DAC 输出以运行回读为准。'
      : '请填写测试电位与 WE 电位，以计算 RE。';
    const fsr = Number(element('htSharedFsr').value);
    for (const option of element('htSharedOffset').options) option.textContent = option.value.endsWith('pct') ? option.value.slice(0, -3) + '% FSR (' + fsr * Number(option.value.slice(0, -3)) / 100 + ' nA)' : option.value.slice(0, -2) + ' nA';
  }
  function renderSampling() {
    if (!model) return;
    const shared = sharedSamplingParameters(model.document), draft = draftAreas.has('sampling');
    element('htSharedSamplingState').textContent = (draft ? '采样参数草稿尚未应用 · ' : '') + (shared.shared ? '两板当前模板参数一致' : '两板参数不同 · 下方以板 A 为起点，点击“应用到两块板”才覆盖两板') + ' · 尚未写入硬件';
    element('htSharedSamplingState').classList.toggle('ht-sampling-warning', !shared.shared || draft);
    if (!draft) {
      element('htSharedPotential').value = shared.settings.potential_v; element('htSharedWorkingElectrode').value = shared.settings.working_electrode_v;
      element('htSharedPeriod').value = String(shared.settings.sens_period_code); element('htSharedFsr').value = String(shared.settings.fsr_nA); element('htSharedOffset').value = shared.settings.offset_mode;
    }
    renderSamplingPotential(); renderBoardConnections();
  }
  function boardConnection(address) {
    const identity = String(address || '').toUpperCase();
    const runtime = Object.values(boards.runtime_boards || {}).find(board => board.address?.toUpperCase() === identity);
    if (runtime && ['acquiring', 'reconnecting', 'error'].includes(runtime.state)) return {state: runtime.state === 'error' ? 'output_unknown' : runtime.state, error: runtime.error?.message};
    const device = (boards.devices || []).find(item => item.address?.toUpperCase() === identity);
    return Object.entries(boards.connections || {}).find(([key]) => key.toUpperCase() === identity)?.[1]
      || {state: device?.connection_state || 'disconnected', error: device?.connection_error, protocol_verified: device?.protocol_verified};
  }
  function updateBoardControls() {
    const blocked = !model || boardsPending || boardsUnavailable || boards.busy === true;
    for (const slot of ['A', 'B']) {
      element('htBoardSelect-' + slot).disabled = blocked;
      const address = boards.bindings?.[slot]?.address || '';
      element('htBoardDisconnect-' + slot).disabled = blocked || !address || !['connected', 'error'].includes(boardConnection(address).state);
    }
    element('htBoardScan').disabled = boardsPending || boards.busy === true;
    element('htBoardScan').textContent = boardsPending === 'scan' ? '扫描中…' : '扫描蓝牙板卡';
    element('htBoardBind').disabled = blocked || !boardDraftDirty || jsonDirty || draftAreas.has('bindings');
    element('htBoardConnect').disabled = blocked || boardDraftDirty || !['A', 'B'].some(slot => boards.bindings?.[slot]?.address);
    element('htBoardConnect').textContent = boardsPending === 'connect' ? '正在连接…' : '连接已绑定板卡';
  }
  function renderBoardConnections() {
    if (!boardDraftDirty) boardDraft = Object.fromEntries(['A', 'B'].map(slot => [slot, model?.document.bindings?.boards?.[slot]?.address || boards.bindings?.[slot]?.address || '']));
    for (const slot of ['A', 'B']) {
      const select = element('htBoardSelect-' + slot), address = boardDraft?.[slot] || '';
      const candidates = new Map();
      for (const device of boards.devices || []) {
        if (device.kind === 'ble' && device.address && !candidates.has(device.address.toUpperCase())) candidates.set(device.address.toUpperCase(), device);
      }
      if (address && !candidates.has(address.toUpperCase())) candidates.set(address.toUpperCase(), {address, name: '已保存 / 暂未发现'});
      const empty = node('option', '', '不使用此板卡槽位'); empty.value = '';
      select.replaceChildren(empty);
      for (const device of candidates.values()) {
        const option = node('option', '', (device.name || '未命名板卡') + ' · ' + device.address); option.value = device.address; select.append(option);
      }
      select.value = candidates.get(address.toUpperCase())?.address || '';
      element('htBoardIdentity-' + slot).textContent = address || '此槽位未绑定';
      const boundAddress = boards.bindings?.[slot]?.address || '', connection = boardConnection(boundAddress);
      const status = element('htBoardConnection-' + slot);
      const stateText = {connected: '已连接', disconnected: '未连接', connecting: '连接中', error: '连接失败', acquiring: '采集中', reconnecting: '掉线 · 正在重连', output_unknown: '故障 / 输出状态未知，请先处理停止确认'}[connection.state] || '状态未确认';
      status.textContent = boardsUnavailable ? '连接状态暂不可用' : !boundAddress ? '未绑定板卡' : stateText + (connection.protocol_verified && connection.state === 'connected' ? ' · 协议已验证' : '')
        + (connection.error ? ' · ' + (connection.error.message || String(connection.error)) : '') + (boundAddress.toUpperCase() !== address.toUpperCase() ? ' · 当前保存的 UUID：' + boundAddress : '');
      status.classList.toggle('ht-board-warning', ['error', 'output_unknown', 'reconnecting'].includes(connection.state) || boardsUnavailable);
    }
    const conflict = boardDraft?.A && boardDraft.A.toUpperCase() === boardDraft.B?.toUpperCase();
    element('htBoardNotice').textContent = conflict ? '同一 BLE 地址不能同时绑定前板 A 与后板 B；请调整后保存。' : boardNotice || (boards.busy ? '板卡采集、设置或恢复进行中，暂不能修改连接。' : boardDraftDirty ? 'A / B 对应草稿未保存；可同时交换两个槽位后保存。' : '按完整 UUID 人工指定前后板，再连接已绑定板卡。');
    element('htBoardNotice').classList.toggle('ht-board-warning', !!conflict || boardNoticeError);
    updateBoardControls();
  }
  async function refreshBoards() {
    if (boardsPolling || boardsPending || document.hidden || document.visibilityState === 'hidden' || !root.classList.contains('active') || element('htMappingPanel').hidden) return;
    const revision = boardsRevision; boardsPolling = true;
    try {
      const next = await api('boards');
      if (revision !== boardsRevision) return;
      boards = next; boardsUnavailable = false;
    } catch (error) {
      if (revision !== boardsRevision) return;
      boardsUnavailable = true; boardNotice = error.message || String(error); boardNoticeError = true;
    } finally { boardsPolling = false; renderBoardConnections(); }
  }
  async function boardAction(action, payload) {
    if (boardsPending || boards.busy === true || (action !== 'scan' && boardsUnavailable)) return;
    if (action === 'bind') {
      if (jsonDirty || draftAreas.has('bindings')) { boardNotice = '请先应用高级设备绑定或 JSON 草稿。'; boardNoticeError = true; renderBoardConnections(); return; }
      if (payload.addresses.A && payload.addresses.A.toUpperCase() === payload.addresses.B.toUpperCase()) { renderBoardConnections(); return; }
    }
    boardsPending = action; boardsRevision++; boardNotice = ''; boardNoticeError = false; updateControls();
    try {
      boards = await api('boards/' + action, payload); boardsUnavailable = false;
      if (action === 'bind') {
        const templateWasDirty = documentDirty, previousEditorState = element('htEditorState').textContent;
        model.edit(document => {
          document.bindings.boards ||= {};
          for (const slot of ['A', 'B']) document.bindings.boards[slot] = {...document.bindings.boards[slot], address: boards.bindings?.[slot]?.address || ''};
        });
        for (const slot of ['A', 'B']) boardAddressEdits.add(slot);
        boardDraftDirty = false; draftAreas.delete('boardMapping'); documentChanged();
        documentDirty = templateWasDirty;
        element('htEditorState').textContent = templateWasDirty || draftAreas.size || normalDrafts.size ? previousEditorState : '模板已保存 · A / B 对应已更新';
        for (const slot of ['A', 'B']) element('htBoardAddress-' + slot).value = model.document.bindings.boards[slot].address;
        boardNotice = templateWasDirty || draftAreas.size || normalDrafts.size ? 'A / B 地址对应已保存；其它模板编辑仍需应用并保存模板。' : 'A / B 地址对应已保存。';
      } else if (action === 'scan') boardNotice = '扫描完成 · 发现 ' + (boards.devices || []).length + ' 块板卡；前后位置由你指定。';
      else boardNotice = action === 'connect' ? '连接请求已完成，请核对每块板卡状态。' : '已完成本板断开请求。';
      if (Object.keys(boards.errors || {}).length) {
        boardNotice = Object.entries(boards.errors).map(([address, error]) => address + '：' + (error.message || String(error))).join('；'); boardNoticeError = true;
      }
    } catch (error) { boardNotice = error.message || String(error); boardNoticeError = true; }
    finally { boardsPending = false; renderBoardConnections(); updateControls(); }
  }
  function renderCalibration() {
    const calibration = guided.calibration, manual = guided.manual || {};
    if (manual.status === 'cancelled' && calibrationOperation?.route !== 'cancel') calibrationOperation = null;
    if (!manual.active && calibration?.status === 'stale' && calibrationOperation?.phase !== 'pending') calibrationOperation = null;
    if (calibrationOperation?.fromBackend && !manual.error) calibrationOperation = null;
    if (calibrationOperation && calibrationOperation.context !== calibrationOperationContext()
      && !(calibrationOperation.route === 'begin' && calibrationOperation.phase === 'pending')) calibrationOperation = null;
    if (calibrationOperation?.phase === 'error' && calibrationOperation.points !== JSON.stringify(manual.points || {})) calibrationOperation = null;
    if ((!calibrationOperation || calibrationOperation.phase === 'success' || calibrationOperation.fromBackend) && manual.error) calibrationOperation = {
      route: String(manual.error.path || '').startsWith('calibration.') || ['geometry_error', 'calibration_height', 'calibration_points', 'invalid_value'].includes(manual.error.code) ? 'finish' : 'operation',
      phase: 'error', error: manual.error, fromBackend: true,
      context: calibrationOperationContext(), points: JSON.stringify(manual.points || {}),
    };
    calibrationErrorDetails.hidden = !manual.error;
    calibrationErrorText.textContent = manual.error ? JSON.stringify(manual.error, null, 2) : '';
    const identity = calibration?.id || calibration?.calibration_id;
    const context = calibrationMotion(), token = context?.motion.connection_token || null;
    if (token !== calibrationConnectionToken) {
      element('htCalibrationReleaseConfirmed').checked = false; element('htCalibrationZSupported').checked = false;
      element('htCalibrationMotionConfirmed').checked = false; jogSettingsDirty = false; jogOrigin = null;
      calibrationConnectionToken = token;
    }
    const axes = ['X', 'Z'].map(axis => {
      const feedback = calibrationAxisState(axis);
      return axis + (feedback ? feedback.enabled ? ' 已锁轴' : ' 已松轴' : ' 状态未确认') + (feedback ? feedback.speed_rpm === 0 ? ' · 静止' : ' · 运动中' : '');
    }).join(' / ');
    element('htCalibrationAxisStatus').textContent = (context ? '当前实读 · ' : snapshot.motion?.connected ? '总线已连接，反馈待确认 · ' : '总线未连接 · ') + axes;
    const ready = guided.four_point_required === true ? fourPointCalibrationReady() : calibration?.usable === true;
    const savedReady = ready && calibration?.four_point === true && calibration.status !== 'stale'
      && !['cancelled', 'blocked', 'error'].includes(manual.status);
    const displayReady = guided.four_point_required === true ? savedReady : ready;
    element('htCalibrationSummary').textContent = (manual.active ? '四点标定中 · 实物实验已阻止'
      : calibration ? (calibration.four_point === true ? '已保存四点标定 ' : '历史标定 ') + (identity || '') + ' · ' + (displayReady ? '当前连接已确认' : '需完成当前连接四点标定与原位锁轴')
      : '尚未完成四点标定 · 可先使用模拟实验') + ' · ' + axes;
    const blocked = manual.status === 'blocked', targetMismatch = /target/i.test(manual.error?.code || '');
    const interrupted = manual.error?.code === 'manual_interrupted';
    const currentManual = manual.active === true && !stateUnavailable && !interrupted
      && !['blocked', 'error', 'cancelled', 'idle', 'complete'].includes(manual.status)
      && (!manual.connection_token || manual.connection_token === token)
      && (!keyboardMode() || !!manual.session_token && manual.session_token === manual.alignment?.session_token);
    const storedMotion = devicesCache?.snapshot?.motion || snapshot.motion;
    const retainedManual = !currentManual && manual.active === true && !interrupted
      && !['cancelled', 'idle', 'complete'].includes(manual.status)
      && !!manual.connection_token && manual.connection_token === storedMotion?.connection_token
      && !!manual.session_token && manual.session_token === manual.alignment?.session_token
      && (!storedMotion?.manual?.session_token || manual.session_token === storedMotion.manual.session_token);
    const points = currentManual || retainedManual ? manual.points || {} : !manual.active && savedReady ? calibration.points || {} : {};
    const captured = CALIBRATION_POINTS.map(([key, title]) => {
      const axis = key === 'first' || key === 'last' ? 'X' : 'Z', raw = points[key]?.raw_counts?.[axis];
      const recorded = Number.isFinite(raw), button = element('htCapture' + key[0].toUpperCase() + key.slice(1));
      button.classList.toggle('ht-calibration-recorded', recorded && !retainedManual);
      button.classList.toggle('ht-calibration-retained', recorded && retainedManual);
      button.textContent = recorded ? '✓ ' + title + ' · 已记录' + (retainedManual ? '（待恢复核对）' : currentManual ? '（重新读取）' : '') : '读取 ' + title;
      button.title = recorded ? title + ' · ' + axis + ' 原始计数 ' + raw + (retainedManual ? ' · 记录已保留，待恢复核对，尚不可用于运行' : '') : title + ' · 本次尚未记录';
      button.setAttribute('aria-label', button.textContent + (recorded ? '，' + axis + ' 原始计数 ' + raw : ''));
      return {recorded, text: title + '：' + (recorded ? '已记录（' + axis + ' ' + raw + ' counts）' + (retainedManual ? ' · 待恢复核对' : '') : '未记录')};
    });
    const capturedCount = captured.filter(point => point.recorded).length;
    calibrationCompletion.hidden = capturedCount !== 4 && !calibrationOperation && !(retainedManual && capturedCount);
    calibrationCompletion.classList.toggle('ht-calibration-complete', savedReady && !manual.active && capturedCount === 4);
    calibrationCompletion.classList.toggle('ht-calibration-failed', calibrationOperation?.phase === 'error');
    calibrationCompletion.classList.toggle('ht-calibration-pending', calibrationOperation?.phase === 'pending');
    calibrationCompletion.setAttribute('aria-busy', String(calibrationOperation?.phase === 'pending'));
    calibrationCompletion.textContent = savedReady && !manual.active && capturedCount === 4
      ? '✓ 四点标定已完成并保存 · 当前连接可用'
      : currentManual && capturedCount === 4 ? '四点已记录 · 待点击「完成四点标定」验证并保存'
      : retainedManual && capturedCount ? '已保留 ' + capturedCount + ' / 4 个采点记录 · 待恢复核对，尚未完成标定。' : '';
    if (calibrationOperation?.phase === 'pending') {
      const title = CALIBRATION_POINTS.find(([point]) => point === calibrationOperation.point)?.[1] || '';
      calibrationCompletion.textContent = calibrationOperation.route === 'capture' ? '正在读取 ' + title + '…请等待服务端确认。'
        : calibrationOperation.route === 'finish' ? '正在验证并保存四点标定…请等待完成结果。'
        : calibrationOperation.route === 'begin' ? '正在检查设备并开始本次标定…' : '正在停止并取消标定…';
    } else if (calibrationOperation?.phase === 'error') {
      const error = calibrationOperation.error, point = String(error.path || '').match(/^calibration\.(first|last|working|safe)\./)?.[1];
      const title = CALIBRATION_POINTS.find(([key]) => key === point)?.[1];
      const reason = title ? title + ' 校验未通过：' + error.message : errorGuidance(error, manual, snapshot);
      const retained = CALIBRATION_POINTS.filter(([key]) => Number.isFinite(manual.points?.[key]?.raw_counts?.[key === 'first' || key === 'last' ? 'X' : 'Z'])).length;
      calibrationCompletion.textContent = (calibrationOperation.route === 'finish' ? '完成标定失败，尚未保存。' : calibrationOperation.route === 'capture' ? '本次读取失败。' : '标定操作失败。')
        + reason + (retained ? ' 服务端仍保留 ' + retained + ' 个采点记录；处理问题后可重试。' : ' 请处理问题后重试。');
    } else if (calibrationOperation?.phase === 'success' && capturedCount !== 4) {
      calibrationCompletion.textContent = calibrationOperation.route === 'capture' ? '✓ ' + (CALIBRATION_POINTS.find(([point]) => point === calibrationOperation.point)?.[1] || '') + ' 已记录 · 当前 ' + capturedCount + ' / 4'
        : calibrationOperation.route === 'cancel' ? '标定已取消，本次点位不可用于实验。'
        : calibrationOperation.route === 'begin' ? '已进入本次标定，请依次读取四点。' : '完成请求已返回，正在等待当前连接的保存状态复核。';
    }
    const phase = interrupted ? '上次标定已中断，请重新进入键盘标定；勿沿用旧点位'
      : blocked ? keyboardMode() ? '键盘标定受阻，完成尚未确认' : '原位锁轴受阻，完成尚未确认'
      : manual.active ? keyboardMode() ? '键盘对位中，请保持供电与现场观察' : '手动对位中，请保持供电并支撑 Z'
        : displayReady ? '本次四点标定已完成；开始实物实验后先到安全高度，再横移，此刻不自动移动' : '尚未进入键盘标定';
    element('htCalibrationProgress').textContent = phase + (retainedManual ? ' · 本次保留记录 ' : ' · 本次有效记录 ') + capturedCount + ' / 4 · ' + captured.map(point => point.text).join(' / ')
      + (targetMismatch ? keyboardMode() ? ' · 控制器目标与当前位置不一致或未确认，禁止使能；请重新上电后勿再手拧轴'
        : ' · 控制器目标与当前位置不一致或未确认，未发送位移；请保持供电并支撑 Z' : '')
      + (manual.error ? ' · ' + errorGuidance(manual.error, manual, snapshot) : '');
    if (!manual.active && manual.boundary_at_current) {
      element('htCalibrationProgress').textContent += ' · 进入后启用当前位置单向保护：' + manual.boundary_at_current.axis
        + ' ' + ({up: '禁止继续上行，允许下退', down: '禁止继续下行，允许上退', left: '禁止继续左行，允许右退', right: '禁止继续右行，允许左退'}[manual.boundary_at_current.direction] || '');
    }
    element('htLegacyManualConfirmations').hidden = true;
    element('htKeyboardConfirmation').hidden = !keyboardMode();
    element('htKeyboardAlignment').hidden = !keyboardMode();
    element('htCalibrationBegin').textContent = keyboardMode() ? keyboardRestartAvailable() ? '重新开始键盘标定（旧点作废）'
      : !snapshot.motion?.connected ? '连接并开始键盘标定' : '进入键盘标定' : '开始历史手动标定';
    const alignment = manual.alignment || {}, settings = alignment.settings || {};
    if (!jogSettingsDirty) {
      element('htJogSpeed').value = String(settings.speed_mm_s ?? 0.5);
      element('htJogMaxSpeed').value = String(settings.max_speed_mm_s ?? 1);
      element('htJogFastConfirmed').checked = settings.fast_motion_confirmed === true;
    }
    element('htJogSpeed').max = jogSettingsDirty ? element('htJogMaxSpeed').value : String(settings.max_speed_mm_s ?? 1);
    element('htJogMaxSpeed').max = String(Math.min(20, settings.hard_max_speed_mm_s ?? 20));
    element('htJogFastClearance').hidden = !(Number(element('htJogMaxSpeed').value) > 2);
    const stoppedByLimit = alignment.status !== 'holding' && ['hold_limit', 'travel_limit', 'lease_expired'].includes(alignment.last_stop_reason);
    const reachedBoundaries = (alignment.boundaries || []).filter(boundary => boundary.at_limit);
    const boundaryNames = {left: 'X 向左（按键 A）', right: 'X 向右（按键 D）', up: 'Z 向上（按键 W）', down: 'Z 向下（按键 S）'};
    if (alignment.status === 'holding') jogNotice = '';
    element('htJogStatus').textContent = blocked ? errorGuidance(manual.error || {code: 'alignment_unavailable'}, manual, snapshot) : (stoppedByLimit ? alignment.last_stop_reason === 'travel_limit' ? '该方向已被保护条件停止，松键后可向反方向移动；请核对下方来源，不是机械终点检测'
      : alignment.last_stop_reason === 'hold_limit' ? '旧会话的固定按住上限已移除，请松开方向键后重按'
      : '续约超时，已停止；请松开所有方向键后重按' : alignment.status === 'holding' ? '正在按住对位 · ' + (alignment.axis || '') + ' ' + ({left: '左', right: '右', up: '上', down: '下'}[alignment.direction] || '')
        : jogNotice || (manual.active ? '按住 WASD / 方向键或箭头对位，松开停止' : '进入键盘标定后可按住方向对位'))
      + (alignment.error ? ' · ' + errorGuidance(alignment.error, manual, snapshot) : '');
    if (!blocked && reachedBoundaries.length) {
      element('htJogStatus').textContent = reachedBoundaries.map(boundary =>
        (boundary.kind === 'operator' ? '已到你设置的方向保护：' : boundary.kind === 'protocol' ? '已到控制器计数范围：' : '旧版软件边界：')
        + boundaryNames[boundary.direction]).join('；')
        + '；仅禁止继续越界，反方向与另一轴仍可用。不是机械限位开关反馈。';
    }
    const counts = Object.fromEntries(['X', 'Z'].map(axis => [axis, calibrationAxisState(axis)?.raw_count]));
    if (manual.active && keyboardMode() && !jogOrigin && ['X', 'Z'].every(axis => Number.isFinite(counts[axis]))) jogOrigin = {...counts};
    element('htJogFeedback').textContent = '编码器实读 · ' + ['X', 'Z'].map(axis => {
      const feedback = calibrationAxisState(axis), raw = feedback?.raw_count;
      const lead = model?.document.bindings?.motion?.axes?.[axis]?.lead_mm;
      const moved = jogOrigin && Number.isFinite(raw) && Number.isFinite(lead) ? (raw - jogOrigin[axis]) * lead / 65536 : null;
      return axis + ' ' + (Number.isFinite(raw) ? raw + ' counts' : '未确认') + (moved === null ? '' : ' · 本次净位移 ' + moved.toFixed(4) + ' mm');
    }).join(' / ') + '；以新鲜反馈为准';
    updateControls();
  }
  function acceptLimits(next) {
    if (!next || typeof next.state !== 'string' || typeof next.active !== 'boolean' || typeof next.calibrating !== 'boolean') throw failure('LIMITS_STATE_INVALID', '限位状态未确认，实物运行受阻');
    if (!next.calibrating || !limits.calibrating) element('htLimitsClearance').checked = false;
    if (JSON.stringify(next.record) !== JSON.stringify(limits.record)) element('htTwinConfirm').checked = false;
    limits = next; limitsUnavailable = false; limitsUncertain = false; limitsReadError = null; renderLimits();
  }
  function renderLimits() {
    const count = LIMIT_BOUNDARIES.filter(([boundary]) => Number.isFinite(limits.draft?.captures?.[boundary]?.raw_counts?.[boundary.startsWith('x_') ? 'X' : 'Z'])).length;
    let summary = limits.record || limits.state === 'stale' ? '需重标 · 已保存记录未在当前连接验证，须重新读取四点' : '未标定 · 实物运动受阻，可先使用模拟实验';
    if (limitsActive()) summary = '当前连接有效 · 四边界限位已验证';
    if (limits.calibrating) summary = '读取中 · 已读取 ' + count + ' / 4 · 实验与普通轴操作已暂停';
    if (limits.error) summary = '故障 · 限位验证失败，实物运动受阻';
    if (limitsUnavailable) summary = limitsReadError ? '故障 · 限位状态读取失败，实物运动受阻' : '正在读取限位状态…';
    element('htLimitsSummary').textContent = summary;
    for (const [boundary] of LIMIT_BOUNDARIES) {
      const captured = limits.draft?.captures?.[boundary]?.raw_counts?.[boundary.startsWith('x_') ? 'X' : 'Z'];
      const saved = limits.record?.boundaries?.[boundary];
      element('htLimitValue-' + boundary).textContent = limits.calibrating ? Number.isFinite(captured) ? '已读取 · 原始计数 ' + captured : '未读取' : Number.isFinite(saved) ? '已保存 · 原始计数 ' + saved : '未读取';
    }
    const error = limitsReadError || limits.error;
    element('htLimitsProgress').textContent = error ? [error.code, error.message || String(error)].filter(Boolean).join(' · ') : limits.calibrating ? '请在每个安全运行边界停稳后读取；四点齐全且确认余量后才可保存。' : !snapshot.motion?.connected ? '请先在高级设置连接X/Z共享RS485滑台，再开始读取；不会自动连接或使能。' : '点击开始后才读取边界；页面刷新不会采集位置或移动电机。';
    const record = limits.record, bounds = record?.boundaries, axes = record?.motion?.axes;
    const spans = bounds && axes ? {X: (bounds.x_left - bounds.x_right) * axes.X?.lead_mm / 65536, Z: (bounds.z_down - bounds.z_up) * axes.Z?.lead_mm / 65536} : {};
    element('htLimitsSpan').textContent = Number.isFinite(spans.X) && Number.isFinite(spans.Z) ? '已保存跨度 · X ' + spans.X.toFixed(3) + ' mm · Z ' + spans.Z.toFixed(3) + ' mm' : '';
    const binding = limits.draft?.motion || record?.motion;
    element('htLimitsBinding').textContent = binding ? '绑定总线：' + binding.port + ' · ' + binding.baud + ' baud · X 地址 ' + (binding.axes?.X?.address ?? '未确认') + ' / Z 地址 ' + (binding.axes?.Z?.address ?? '未确认') + (limitsActive() ? ' · 当前连接已验证' : ' · 当前连接尚未验证') : '限位与实际总线、轴配置及连接身份绑定，实验 JSON 无法扩大边界。';
    renderTwin();
  }
  const twinFields = [['x_left_mm', 'htTwinXLeft'], ['x_right_mm', 'htTwinXRight'], ['z_up_lift_mm', 'htTwinZUp'], ['z_down_lift_mm', 'htTwinZDown']];
  const twinDatumCorners = {left_up: ['x_left', 'z_up'], right_down: ['x_right', 'z_down']};
  function twinFieldsComplete() { return twinFields.every(([, id]) => element(id).value.trim() !== '' && Number.isFinite(Number(element(id).value))); }
  function sameTwinContext(left, right) {
    if (left === right) return true;
    if (!left || !right || typeof left !== 'object' || typeof right !== 'object' || Array.isArray(left) !== Array.isArray(right)) return false;
    const leftKeys = Object.keys(left).sort(), rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && sameTwinContext(left[key], right[key]));
  }
  function twinMotionMatches(binding) {
    const motion = devicesCache?.snapshot?.motion;
    if (!binding || typeof binding !== 'object' || typeof binding.connection_token !== 'string'
      || !binding.connection_token || binding.connection_token !== motion?.connection_token) return false;
    const identity = {port: motion.port, baud: motion.baud, axes: {}}, profiles = {};
    for (const axis of ['X', 'Z']) {
      identity.axes[axis] = motion.axes?.[axis]?.mechanical;
      profiles[axis] = Object.fromEntries(['firmware_version', 'hardware_version', 'config', 'power_flag_baseline'].map(field => [field, motion.axes?.[axis]?.[field]]));
    }
    return sameTwinContext(binding.motion, identity) && sameTwinContext(binding.profiles, profiles);
  }
  function independentTwinDatum() {
    const registration = twin.registration;
    return registration?.version === 'digital-twin-datum-1' && registration.basis === 'nominal_module_endpoints'
      && registration.display_only === true && registration.accuracy_unverified === true;
  }
  function twinContextMatches() {
    if (twin.registration?.version === 'digital-twin-experiment-1') return twinMotionMatches(twin.registration.binding)
      && guided.calibration?.usable === true && guided.calibration.id === twin.registration.calibration_id
      && devicesCache?.snapshot?.calibration?.display_valid === true;
    if (independentTwinDatum()) return twinMotionMatches(twin.registration.binding);
    const record = twin.registration?.limits_record, currentLimits = devicesCache?.snapshot?.limits, motion = devicesCache?.snapshot?.motion;
    return !!record && limitsActive() && sameTwinContext(record, limits.record)
      && devicesCache?.available === true && currentLimits?.active === true && currentLimits.state === 'active' && !currentLimits.calibrating && !currentLimits.error
      && sameTwinContext(record, currentLimits.record) && sameTwinContext(record, motion?.absolute_limits) && twinMotionMatches(record);
  }
  function healthyTwinMotion() {
    const motion = devicesCache?.snapshot?.motion, receivedAt = devicesCache?.received_at_ms;
    const elapsed = Math.max(0, (Date.now() - receivedAt) / 1000);
    return devicesCache?.available === true && Number.isFinite(receivedAt) && receivedAt > 0
      && motion?.connected === true && motion.faulted === false && !motion.feedback_error
      && ['X', 'Z'].every(axis => motion.axes?.[axis]?.telemetry_valid === true && Number.isFinite(motion.axes[axis].feedback_age_s)
        && motion.axes[axis].feedback_age_s >= 0 && motion.axes[axis].feedback_age_s + elapsed <= 2
        && Number.isInteger(motion.axes[axis].raw_count) && Math.abs(motion.axes[axis].raw_count) <= 0xffffffff);
  }
  function twinDatumSource(datum = element('htTwinDatumCorner').value) {
    const boundaries = twinDatumCorners[datum];
    if (!boundaries || limitsUnavailable || limitsUncertain || limits.error || !healthyTwinMotion()) return null;
    const source = limits.calibrating ? limits.draft : limitsActive() ? limits.record : null;
    if (!twinMotionMatches(source)) return null;
    const counts = Object.fromEntries(boundaries.map((boundary, index) => [index === 0 ? 'X' : 'Z', limits.calibrating
      ? source.captures?.[boundary]?.raw_counts?.[index === 0 ? 'X' : 'Z'] : source.boundaries?.[boundary]]));
    return Object.values(counts).every(value => Number.isInteger(value) && Math.abs(value) <= 0xffffffff) ? {source, counts} : null;
  }
  function twinDatumInteractionAllowed() {
    return !stateUnavailable && !guided.manual?.active && !snapshot.recovery_required && ['idle', 'completed', 'stopped'].includes(snapshot.state)
      && snapshot.stop_confirmed === true
      && (!snapshot.busy || limits.calibrating);
  }
  function canRegisterTwinDatum() {
    return twinDatumInteractionAllowed() && !!twinDatumSource();
  }
  function validTwinPose() {
    const pose = twin.pose, elapsed = Math.max(0, (Date.now() - twinReceivedAt) / 1000);
    return !twinReadError && !twin.error && healthyTwinMotion() && twinContextMatches() && twin.state === 'synced' && twin.registered === true && twin.fresh === true
      && pose?.source === 'encoder_feedback' && ['scene_x_mm', 'scene_z_mm', 'x_center_mm', 'lift_mm'].every(key => Number.isFinite(pose[key]))
      && ['X', 'Z'].every(axis => Number.isFinite(pose.feedback_age_s?.[axis]) && pose.feedback_age_s[axis] >= 0 && pose.feedback_age_s[axis] + elapsed <= 2);
  }
  function physicalAttached() {
    if (devicesCache?.snapshot?.motion?.connected === true || validTwinPose()) return true;
    return devicesCache?.available === true ? false : snapshot.motion?.connected === true;
  }
  function renderTwinRegistration() {
    const error = twinReadError || twin.error;
    if (!twinDatumSelected) element('htTwinDatumCorner').value = !twinDatumSource('left_up') && twinDatumSource('right_down') ? 'right_down' : 'left_up';
    const datum = element('htTwinDatumCorner').value, captured = twinDatumSource(datum);
    const signature = captured ? JSON.stringify({datum, connection_token: captured.source.connection_token, motion: captured.source.motion, profiles: captured.source.profiles, counts: captured.counts}) : null;
    if (signature !== twinDatumSignature) { element('htTwinDatumConfirm').checked = false; twinDatumSignature = signature; }
    element('htTwinRegisterDatum').textContent = datum === 'right_down' ? '用已读取的X右/Z下建立3D基准' : '用已读取的X左/Z上建立3D基准';
    element('htTwinDatumStatus').textContent = independentTwinDatum() ? '已保存名义CAD端点基准 · 非机械精度验收 · ' + (validTwinPose() ? '反馈有效' : '当前反馈或连接待核对')
      : error ? '3D基准尚未确认 · ' + (error.message || error.code || String(error))
      : captured ? '所选两轴边界已读取 · 确认后仅建立显示基准，运行限位门禁保持'
      : '请先读取当前连接所选的两个边界；需要新鲜且配置一致的双轴反馈';
    element('htTwinRegisterStatus').textContent = !limitsActive() ? '请先读取并保存当前连接的四边界限位；缺少有效限位时无法配准。'
      : error ? 'CAD 对应尚未确认 · ' + (error.message || error.code || String(error))
      : twin.registered && !independentTwinDatum() ? '已保存 CAD 对应 · 跨度配准容差 0.1 mm · 当前状态：' + (validTwinPose() ? '反馈有效' : twin.state === 'disconnected' ? '总线已断开' : '需核对配准或反馈')
      : '四边界限位有效；请填写并确认各边界的 CAD 坐标后保存。';
  }
  function renderTwin() {
    const attached = physicalAttached(), valid = validTwinPose(), error = twinReadError || twin.error;
    scene?.setPhysicalAttached?.(attached);
    let caption;
    if (valid) {
      scene?.stopPreview(); scene?.setFeedbackPose?.(twin.pose);
      const pose = twin.pose, age = (Math.max(pose.feedback_age_s.X, pose.feedback_age_s.Z) + Math.max(0, (Date.now() - twinReceivedAt) / 1000)).toFixed(1);
      caption = '编码器反馈 · ' + (twin.registration?.version === 'digital-twin-experiment-1' ? '实验四点基准 · ' : independentTwinDatum() ? '名义CAD端点基准 · ' : '') + '孔板中心 ' + pose.x_center_mm.toFixed(3) + ' mm · lift ' + pose.lift_mm.toFixed(3) + ' mm · 反馈年龄 ' + age + ' s' + (twin.registration?.version === 'digital-twin-experiment-1' ? ' · Z浸入姿态按CAD示意' : independentTwinDatum() ? ' · 非机械精度验收' : '');
      element('htPose').textContent = caption;
    } else {
      if (attached || twin.registered) { scene?.stopPreview(); scene?.clearFeedbackPose?.(); }
      element('htPose').textContent = 'CAD 参考姿态 · 当前实物位置未知';
      const reason = error ? '反馈 / 配准故障' : twin.state === 'stale' || twin.state === 'synced' ? '反馈过期或配准需核对' : twin.state === 'disconnected' ? 'RS485 已断开' : '3D 未配准';
      caption = reason + ' · 实物位置未知 · CAD 参考姿态' + (error ? ' · ' + (error.message || error.code || String(error)) : '');
    }
    element('htTwinStatus').textContent = caption; element('htTwinStatus').className = 'ht-twin-status ' + (valid ? 'synced' : 'unknown');
    element('htSimulationNotice').textContent = attached ? '模拟实验 · 生成合成数据，不操作实物。3D 优先显示实物编码器反馈；反馈未知时显示参考姿态。' : '模拟实验 · 生成合成数据，不操作实物。3D 仅作流程示意。';
    renderTwinRegistration(); updateControls();
  }
  async function refreshTwin() {
    if (twinPolling || twinAction) return;
    const revision = twinRevision; twinPolling = true;
    try {
      const next = await api('twin');
      if (!next || typeof next.state !== 'string' || typeof next.registered !== 'boolean' || typeof next.fresh !== 'boolean') throw failure('TWIN_STATE_INVALID', '编码器反馈与 CAD 配准状态未确认');
      if (revision === twinRevision) { twin = next; twinReceivedAt = Date.now(); twinReadError = null; renderTwin(); }
    } catch (error) { if (revision === twinRevision) { twinReadError = error; renderTwin(); } }
    finally { twinPolling = false; }
  }
  async function refreshLimits() {
    if (limitsPolling || limitsAction) return;
    const revision = limitsRevision; limitsPolling = true;
    try { const next = await api('limits'); if (revision === limitsRevision) acceptLimits(next); }
    catch (error) { if (revision === limitsRevision) { limitsUnavailable = true; limitsReadError = error; renderLimits(); } }
    finally { limitsPolling = false; }
  }
  async function changeLimits(route, payload) {
    limitsRevision++; limitsAction = true; element('htLimitsClearance').checked = false;
    try { acceptLimits(await api('limits/' + route, payload)); }
    catch (error) { limitsUnavailable = true; limitsUncertain = true; limitsReadError = error; renderLimits(); throw error; }
    finally { limitsAction = false; void refreshLimits(); }
    await refresh();
  }
  function renderPlan() {
    const plan = model.plan;
    element('htPlanSummary').textContent = (plan.steps?.length || 0) + ' 步 · 预计 ' + Number(plan.duration_s || 0).toFixed(1) + ' s · ' + plan.plan_hash.slice(0, 12);
    element('htTimeline').replaceChildren();
    for (const [index, step] of (plan.steps || []).entries()) {
      const block = node('div', 'ht-step ht-action-' + step.op); block.dataset.stepIndex = index;
      const parameters = {...step.resolved_params}; delete parameters.settings;
      block.append(node('small', '', String(index + 1)), node('strong', '', titles[step.op] || step.op), node('span', '', step.column ? '物理第 ' + step.column + ' 列' : '—'), node('code', '', JSON.stringify(parameters))); element('htTimeline').append(block);
    }
    element('htWarnings').replaceChildren();
    for (const warning of plan.warnings || []) element('htWarnings').append(node('p', '', typeof warning === 'string' ? warning : JSON.stringify(warning)));
  }
  function renderEvents(events) {
    for (const event of events || []) {
      const identity = event.seq ?? JSON.stringify(event);
      if (seenEvents.has(identity)) continue;
      seenEvents.add(identity); if (Number.isFinite(event.seq)) eventSequence = Math.max(eventSequence, event.seq);
      const row = node('div', 'ht-event');
      row.append(node('time', '', event.host_utc || event.utc || event.timestamp || String(event.seq ?? '')), node('strong', '', event.type || event.event || '事件'), node('span', '', event.message || JSON.stringify(event))); element('htEvents').append(row);
    }
    while (element('htEvents').childElementCount > 250) element('htEvents').firstElementChild.remove();
  }
  function renderState(next) {
    snapshot = next; stateReceivedAt = Date.now();
    const total = routeProgressCaption(next);
    element('htTotalPercent').textContent = total.percent === null ? '—' : total.percent.toFixed(1) + '%';
    if (total.percent === null) element('htTotalProgress').removeAttribute?.('value');
    else element('htTotalProgress').value = total.percent;
    element('htTotalRoute').textContent = total.detail;
    element('htTotalTiming').textContent = total.timing;
    const mode = next.mode === 'simulate' ? '模拟实验' : next.mode === 'live' ? '实物实验' : '待机';
    element('htStatus').textContent = mode + ' · ' + (stateNames[next.state] || next.state || '就绪') + (next.message ? ' · ' + next.message : '');
    element('htProgressMode').textContent = mode; element('htPosition').textContent = positionCaption(next);
    const timingCaption = guidedTimingCaption(next);
    element('htClock').textContent = timingCaption.clock; element('htTimingDetail').textContent = timingCaption.detail;
    element('htBoardRecovery').replaceChildren();
    for (const slot of ['A', 'B']) {
      const board = next.boards?.[slot];
      if (!board) continue;
      const row = node('p', 'ht-board-status ' + (board.phase === 'retrying' || board.output_state === 'unknown' ? 'ht-board-warning' : ''));
      row.textContent = '板 ' + slot + ' · ' + (phaseNames[board.phase] || board.phase || '待机') + ' · ' + (board.output_state === 'unknown' ? '输出状态未知' : board.stop_confirmed ? 'STOP 已确认' : 'STOP 未确认') + (board.gaps?.length ? ' · ' + board.gaps.length + ' 段缺口' : '');
      element('htBoardRecovery').append(row);
    }
    element('htRunInfo').textContent = next.run_id ? '运行 ' + next.run_id + ' · ' + (next.stop_confirmed === true ? 'STOP 已确认' : 'STOP 未确认') : '尚无运行';
    element('htManifest').textContent = next.manifest_path || ''; element('htMotionState').textContent = JSON.stringify(next.motion || {connected: false}, null, 2);
    for (const id of CHANNELS) {
      const channel = (next.channels || []).find(item => item.id === id), card = element('htCard-' + id), {current, time} = channelReadout(channel);
      const selected = model?.document.electrodes.find(item => item.id === id)?.enabled !== false;
      const board = next.boards?.[id < 'E' ? 'A' : 'B'];
      card.querySelector('.ht-channel-value').textContent = Number.isFinite(current) ? current.toFixed(3) : '—';
      card.querySelector('.ht-channel-name').textContent = channel?.name || model?.document.electrodes.find(item => item.id === id)?.name || id;
      card.querySelector('.ht-channel-detail').textContent = !selected ? '本模板未选用' : (channel?.synthetic || next.mode === 'simulate' ? '合成数据 · ' : '') + '板 ' + (id < 'E' ? 'A' : 'B') + ' CH' + (CHANNELS.indexOf(id) % 4 + 1) + ' · ' + (time == null ? '尚无样本' : '末次 ' + time + ' s') + (board?.phase === 'retrying' ? ' · 掉线缺数' : '');
      card.classList.toggle('invalid', channel?.valid === false); card.classList.toggle('synthetic', channel?.synthetic === true || next.mode === 'simulate'); card.classList.toggle('ht-unselected', !selected);
    }
    for (const step of element('htTimeline').children) step.classList.toggle('active', Number(step.dataset.stepIndex) === next.step_index && !!next.busy);
    renderEvents(next.events); renderLimits(); renderTwin(); renderCalibration();
  }
  function pendingKeyboardStopSnapshot(next) {
    return keyboardInput.isStopping() && next?.manual?.active === true
      && next.manual.session_token === guided.manual?.session_token
      && !['blocked', 'error'].includes(next.manual.status)
      && ['idle', 'stopping'].includes(next.manual.alignment?.status);
  }
  async function refreshGuided() {
    const revision = calibrationRevision, next = await api('guided');
    if (revision !== calibrationRevision || pendingKeyboardStopSnapshot(next)) return;
    guided = next;
    if (!model) { model = createGuidedModel(guided.document); renderDocument(); element('htEditorState').textContent = '模板已读取'; }
    else syncServerBoardAddresses(guided.document?.bindings?.boards);
    renderCalibration();
  }
  async function refresh() {
    void refreshLimits();
    if (polling) return;
    const revision = calibrationRevision, boardRevision = boardsRevision; polling = true;
    try {
      const [next, currentGuided] = await Promise.all([api('state'), api('guided')]);
      stateUnavailable = false; if (revision === calibrationRevision && !pendingKeyboardStopSnapshot(currentGuided)) guided = currentGuided;
      if (!model) { model = createGuidedModel(guided.document); renderDocument(); element('htEditorState').textContent = '模板已读取'; }
      else if (boardRevision === boardsRevision) syncServerBoardAddresses(guided.document?.bindings?.boards);
      renderCalibration(); renderState(next);
      const events = await api('events?after=' + eventSequence); renderEvents(Array.isArray(events) ? events : events.events);
    } catch (error) { stateUnavailable = true; showError(error); renderCalibration(); }
    finally { polling = false; }
  }
  async function createScene() {
    if (sceneStarted) return;
    sceneStarted = true;
    try {
      scene = await scope.HighThroughputScene.create(element('htStage'), {labels: element('htSceneLabels'), onSelectColumn: selectColumn, onPreview: text => { element('htPose').textContent = text; }});
      element('htSceneLoading').hidden = true; syncScene(); renderTwin();
    } catch (error) { element('htSceneLoading').textContent = '3D 暂不可用：' + error.message + '。仍可使用物理列勾选。'; }
  }
  function numeric(id) {
    const value = element(id).value.trim();
    if (value === '' || !Number.isFinite(Number(value))) throw failure('NUMBER_INVALID', '请输入有限数值', id);
    return Number(value);
  }
  function editNormalField(id, change) {
    try {
      requireDocument('normal');
      if (draftAreas.size) throw failure('FIELDS_NOT_APPLIED', '请先应用已编辑的高级参数或设备绑定');
      change(); normalDrafts.delete(id); documentChanged(); renderColumnHints(); element('htError').hidden = true; element('htErrorDetails').hidden = true;
    } catch (error) {
      normalDrafts.add(id); model?.invalidate(); runKey = null; showError(error); updateControls();
    }
  }
  function updateWindow(id, fieldId) {
    editNormalField(fieldId, () => {
      const column = model.document.columns.find(item => item.index === model.selectedColumn);
      const durationText = element('htWindow-' + id).value.trim(), delayText = element('htDelay-' + id).value.trim();
      const start = delayText ? numeric('htDelay-' + id) : 0;
      if (!durationText && start === 0) model.setWindow(column.index, id, null);
      else {
        const duration = durationText ? numeric('htWindow-' + id) : model.columnDuration(column.index) - start;
        model.setWindow(column.index, id, {start_s: start, duration_s: duration});
      }
    });
  }
  element('htName').addEventListener('input', () => editDocument(() => model.edit(document => { document.name = element('htName').value; })));
  element('htColumnName').addEventListener('input', () => editDocument(() => model.updateColumn(model.selectedColumn, {name: element('htColumnName').value})));
  element('htColumnDuration').addEventListener('input', () => editNormalField('htColumnDuration', () => model.setColumnDuration(model.selectedColumn, element('htColumnDuration').value.trim() ? numeric('htColumnDuration') : null)));
  element('htDefaultDuration').addEventListener('input', () => editNormalField('htDefaultDuration', () => {
    const duration = numeric('htDefaultDuration');
    if (duration <= 0) throw failure('DURATION_INVALID', '默认时长须大于 0');
    for (const column of model.document.columns.filter(item => item.duration_s == null)) for (const window of Object.values(column.windows || {})) if (window.start_s + window.duration_s > duration) throw failure('WINDOW_INVALID', '已有采集窗口超过新的默认时长');
    model.edit(document => { document.defaults.dwell_s = duration; });
  }));
  function showTemplatePane(selected) {
    for (const [name, toggleId, panelId] of [['sampling', 'htSamplingToggle', 'htSamplingPanel'], ['mapping', 'htMappingToggle', 'htMappingPanel']]) {
      element(panelId).hidden = selected !== name; element(toggleId).setAttribute('aria-selected', String(selected === name));
    }
    if (selected === 'mapping') void refreshBoards();
  }
  for (const [name, toggleId, panelId] of [['sampling', 'htSamplingToggle', 'htSamplingPanel'], ['mapping', 'htMappingToggle', 'htMappingPanel']]) element(toggleId).addEventListener('click', () => showTemplatePane(element(panelId).hidden ? name : null));
  for (const slot of ['A', 'B']) {
    element('htBoardSelect-' + slot).addEventListener('change', () => {
      boardDraft ||= {A: '', B: ''}; boardDraft[slot] = element('htBoardSelect-' + slot).value.trim();
      boardDraftDirty = true; draftAreas.add('boardMapping'); boardNotice = ''; boardNoticeError = false;
      model?.invalidate(); runKey = null; element('htLiveConfirmed').checked = false; renderBoardConnections(); updateControls();
    });
    element('htBoardDisconnect-' + slot).addEventListener('click', () => {
      const address = boards.bindings?.[slot]?.address;
      if (address) void boardAction('disconnect', {addresses: [address]});
    });
  }
  element('htBoardScan').addEventListener('click', () => { void boardAction('scan', {}); });
  element('htBoardBind').addEventListener('click', () => { if (model && boardDraftDirty) void boardAction('bind', {addresses: {...boardDraft}}); });
  element('htBoardConnect').addEventListener('click', () => {
    const addresses = ['A', 'B'].map(slot => boards.bindings?.[slot]?.address).filter(Boolean);
    if (!boardDraftDirty && addresses.length) void boardAction('connect', {addresses});
  });
  for (const id of ['htSharedPotential', 'htSharedWorkingElectrode', 'htSharedPeriod', 'htSharedFsr', 'htSharedOffset']) element(id).addEventListener('input', () => {
    draftAreas.add('sampling'); model?.invalidate(); runKey = null; element('htLiveConfirmed').checked = false; scene?.stopPreview();
    element('htEditorState').textContent = '共用采样参数草稿待应用'; renderSampling(); updateControls();
  });
  element('htApplySharedSampling').addEventListener('click', () => handle(async () => {
    requireDocument('sampling');
    if ([...draftAreas].some(area => area !== 'sampling') || normalDrafts.size) throw failure('FIELDS_NOT_APPLIED', '请先应用其它编辑并修正列时长 / 采集窗口');
    model.applySharedSampling({potential_v: numeric('htSharedPotential'), working_electrode_v: numeric('htSharedWorkingElectrode'),
      sens_period_code: numeric('htSharedPeriod'), fsr_nA: numeric('htSharedFsr'), offset_mode: element('htSharedOffset').value});
    draftAreas.delete('sampling'); documentChanged();
    for (const slot of ['A', 'B']) element('htBoardSettings-' + slot).value = JSON.stringify(model.document.bindings.boards[slot].settings, null, 2);
  }));
  for (const [id, enabled] of [['htSelectAllColumns', true], ['htClearColumns', false]]) element(id).addEventListener('click', () => editDocument(() => model.edit(document => { for (const column of document.columns) column.enabled = enabled; }), true));
  function syncServerBoardAddresses(bindings) {
    if (!model || !bindings) return;
    const updates = {};
    for (const slot of ['A', 'B']) {
      const address = bindings[slot]?.address;
      if (typeof address !== 'string') continue;
      const current = model.document.bindings?.boards?.[slot]?.address || '';
      if (current === address) boardAddressEdits.delete(slot);
      if (boardAddressEdits.has(slot) || boardAddressDrafts.has(slot) || current === address) continue;
      updates[slot] = address;
    }
    if (!Object.keys(updates).length) return;
    model.edit(document => {
      document.bindings.boards ||= {};
      for (const [slot, address] of Object.entries(updates)) document.bindings.boards[slot] = {...document.bindings.boards[slot], address};
    });
    runKey = null;
    for (const [slot, address] of Object.entries(updates)) element('htBoardAddress-' + slot).value = address;
    if (!jsonDirty) element('htJson').value = JSON.stringify(model.document, null, 2);
    renderBoardConnections();
  }
  async function saveGuidedDocument() {
    if (snapshot.busy || guided.manual?.active || boardsPending) throw failure('BUSY', '实验或标定进行中，不能保存实验');
    const latest = await api('guided');
    if (snapshot.busy || guided.manual?.active || latest.manual?.active || boardsPending) throw failure('BUSY', '实验或标定进行中，不能保存实验');
    syncServerBoardAddresses(latest.document?.bindings?.boards);
    boardsRevision++;
    const submitted = copy(model.document), submittedRevision = model.serialize();
    const result = await api('guided/experiment', {document: submitted});
    const sameValue = (expected, actual) => {
      if (expected === null || typeof expected !== 'object') return expected === actual;
      if (actual === null || typeof actual !== 'object' || Array.isArray(expected) !== Array.isArray(actual)) return false;
      const keys = Object.keys(expected);
      return keys.length === Object.keys(actual).length && keys.every(key => sameValue(expected[key], actual[key]));
    };
    if (!sameValue(submitted, result?.document)) throw failure('SAVE_NOT_CONFIRMED', '服务器未确认完整参数，尚未应用，请重试。');
    boardAddressEdits.clear(); documentDirty = model.serialize() !== submittedRevision;
    return result;
  }
  function renderDefaultsFeedback() {
    const state = sectionFeedback.defaults, button = element('htApplyDefaults');
    button.textContent = {idle: '应用高级参数', dirty: '待应用 · 高级参数', applying: '应用中…', applied: '✓ 已应用', failed: '应用失败 · 重试'}[state.phase];
    button.className = 'secondary' + (state.phase === 'applied' ? ' is-applied' : '');
    const status = element('htDefaultsApplyStatus');
    status.textContent = state.message; status.className = 'ht-apply-status ' + state.phase;
  }
  function dirtyDefaultsFeedback() {
    const state = sectionFeedback.defaults;
    state.revision++; state.phase = state.phase === 'applying' ? 'applying' : 'dirty';
    state.message = '参数已修改，待应用保存。'; renderDefaultsFeedback();
  }
  function cancelDefaultsFeedback() {
    if (sectionFeedback.defaults.phase !== 'applying') return;
    dirtyDefaultsFeedback(); draftAreas.add('defaults');
  }
  function syncFinishedTravel(next) {
    if (!model || next.manual?.active || next.calibration?.usable !== true || next.calibration?.four_point !== true) return;
    const axes = next.document?.bindings?.motion?.axes;
    if (!axes || !['X', 'Z'].every(axis => Number.isFinite(axes[axis]?.travel_mm) && axes[axis].travel_mm > 0)) return;
    model.edit(document => {
      for (const axis of ['X', 'Z']) document.bindings.motion.axes[axis].travel_mm = axes[axis].travel_mm;
    });
    for (const axis of ['X', 'Z']) element('htAxis-' + axis + '-travel_mm').value = axes[axis].travel_mm;
    if (!jsonDirty) element('htJson').value = JSON.stringify(model.document, null, 2);
    runKey = null;
  }
  function executionSummary(prefix) {
    const execution = model.document.execution || {mode: 'once', count: 1, route: null};
    const route = execution.route || model.selectedColumns().map(column => column.index);
    const name = {once: '一次性实验', repeat: '重复实验', custom: '自定义路径'}[execution.mode];
    return prefix + ' · ' + name + ' · 路径 ' + route.join(' → ') + ' · ' + execution.count + ' 轮 · 下次实验生效 · 各列参数保持不变';
  }
  function renderExecutionInputs() {
    const mode = element('htExecutionMode').value;
    element('htExecutionCountLabel').hidden = mode !== 'repeat';
    element('htExecutionRouteLabel').hidden = mode !== 'custom';
  }
  function executionDraftChanged() {
    if (pending || snapshot.busy || guided.manual?.active || boardsPending || limitCalibrationActive()) return;
    draftAreas.add('execution'); model?.invalidate(); runKey = null;
    element('htApplyExecution').textContent = '待应用 · 执行设置';
    element('htApplyExecution').className = 'secondary';
    element('htLiveConfirmed').checked = false; scene?.stopPreview();
    element('htExecutionStatus').textContent = '执行设置待应用保存；现有列参数保持不变。';
    renderExecutionInputs(); updateControls();
  }
  element('htExecutionMode').addEventListener('change', executionDraftChanged);
  for (const id of ['htExecutionCount', 'htExecutionRoute']) element(id).addEventListener('input', executionDraftChanged);
  element('htApplyExecution').addEventListener('click', () => handle(async () => {
    requireDocument('execution');
    if ([...draftAreas].some(area => area !== 'execution') || normalDrafts.size) throw failure('FIELDS_NOT_APPLIED', '请先应用或修正其他参数草稿');
    const executionDraft = () => JSON.stringify(['htExecutionMode', 'htExecutionCount', 'htExecutionRoute'].map(id => element(id).value));
    const submittedDraft = executionDraft();
    try {
      model.applyExecution(element('htExecutionMode').value, Number(element('htExecutionCount').value), element('htExecutionRoute').value);
      draftAreas.add('execution'); documentChanged(true);
      element('htApplyExecution').textContent = '保存中…';
      element('htApplyExecution').className = 'secondary';
      element('htExecutionStatus').textContent = '正在保存执行设置…';
      await saveGuidedDocument();
      if (executionDraft() !== submittedDraft || documentDirty) throw failure('STALE_DRAFT', '先前设置已保存；当前修改仍待应用，请重新应用执行设置');
      draftAreas.delete('execution');
      element('htExecutionStatus').textContent = executionSummary('执行设置已保存');
      element('htApplyExecution').textContent = '✓ 已应用 · 保存成功';
      element('htApplyExecution').className = 'secondary is-applied';
    } catch (error) {
      element('htApplyExecution').textContent = '应用失败 · 重试';
      element('htApplyExecution').className = 'secondary';
      draftAreas.add('execution'); element('htExecutionStatus').textContent = '未应用保存：' + error.message; throw error;
    }
  }));
  element('htSave').addEventListener('click', () => handle(async () => { requireDocument(); await saveGuidedDocument(); element('htEditorState').textContent = '模板已保存 · 标定保持复用'; }));
  element('htApplyDefaults').addEventListener('click', () => handle(async () => {
    const state = sectionFeedback.defaults, revision = state.revision;
    state.phase = 'applying'; state.message = '正在保存高级参数…'; renderDefaultsFeedback();
    try {
      requireDocument('defaults');
      if (normalDrafts.has('htDefaultDuration')) throw failure('DURATION_INVALID', '请先修正默认采集时长，再应用高级参数。');
      const defaults = copy(model.document.defaults);
      for (const [key] of movementFields) defaults[key] = numeric('htDefault-' + key);
      model.edit(document => {
        document.output_dir = element('htOutput').value; document.defaults = defaults;
        for (const electrode of document.electrodes) electrode.name = element('htElectrode-' + electrode.id).value;
      });
      draftAreas.add('defaults'); documentChanged();
      await saveGuidedDocument();
      if (revision !== state.revision) {
        state.phase = 'dirty'; state.message = '先前参数已保存；当前修改仍待应用。';
      } else {
        draftAreas.delete('defaults'); state.phase = 'applied';
        state.message = '已保存，下一次实验生效 · 横移 ' + defaults.translate_speed_mm_s + ' mm/s · 提拉 ' + defaults.ascend_speed_mm_s
          + ' mm/s · 下降 ' + defaults.descend_speed_mm_s + ' mm/s · 停稳 ' + defaults.settle_s + ' s · 默认采集 ' + defaults.dwell_s + ' s（单列覆盖优先）';
        element('htEditorState').textContent = '高级参数已保存';
      }
    } catch (error) {
      draftAreas.add('defaults'); state.phase = 'failed'; state.message = '保存失败：' + (error.message || String(error)); throw error;
    } finally { renderDefaultsFeedback(); }
  }));
  element('htApplyBindings').addEventListener('click', () => handle(async () => {
    requireDocument('bindings'); const bindings = copy(model.document.bindings);
    for (const slot of ['A', 'B']) {
      const address = element('htBoardAddress-' + slot).value.trim();
      if (address !== (bindings.boards[slot]?.address || '')) boardAddressEdits.add(slot);
      bindings.boards[slot] = {address, settings: JSON.parse(element('htBoardSettings-' + slot).value)};
    }
    bindings.motion.port = element('htPort').value.trim(); bindings.motion.baud = numeric('htBaud');
    for (const axis of ['X', 'Z']) for (const key of axisFields) bindings.motion.axes[axis][key] = numeric('htAxis-' + axis + '-' + key);
    model.edit(document => { document.bindings = bindings; document.safety = {...document.safety, motion_policy_confirmed: element('htMotionPolicy').checked, electrical_policy_confirmed: element('htElectricalPolicy').checked}; });
    boardAddressDrafts.clear(); draftAreas.delete('bindings'); documentChanged(); renderCalibration();
  }));
  element('htJson').addEventListener('input', () => {
    if (!jsonDirty) jsonBoardAddressBase = Object.fromEntries(['A', 'B'].map(slot => [slot, model?.document.bindings?.boards?.[slot]?.address || '']));
    jsonDirty = true; model?.invalidate(); runKey = null; element('htLiveConfirmed').checked = false; scene?.stopPreview(); element('htEditorState').textContent = '高级 JSON 草稿待应用'; updateControls();
  });
  element('htApplyJson').addEventListener('click', () => handle(async () => {
    requireDocument('json'); const draft = element('htJson').value, parsed = JSON.parse(draft), original = model.serialize();
    await api('guided/preview', {document: parsed, mode: 'simulate'});
    if (element('htJson').value !== draft || model.serialize() !== original) throw failure('STALE_DRAFT', '验证期间草稿已变更');
    for (const slot of ['A', 'B']) {
      const address = parsed.bindings?.boards?.[slot]?.address;
      if (typeof address !== 'string') continue;
      if (address !== (jsonBoardAddressBase?.[slot] ?? model.document.bindings?.boards?.[slot]?.address ?? '')) boardAddressEdits.add(slot);
      else parsed.bindings.boards[slot].address = model.document.bindings?.boards?.[slot]?.address || '';
    }
    model.replace(parsed); boardAddressDrafts.clear(); draftAreas.clear(); documentChanged(); renderDocument();
  }));
  async function previewCurrent() {
    requireDocument(); const original = model.serialize(), plan = await api('guided/preview', {document: copy(model.document), mode: requestedMode});
    if (jsonDirty || draftAreas.size) throw failure('STALE_PLAN', '预览期间草稿已变更');
    model.acceptPlan(plan, original, requestedMode); renderPlan(); return plan;
  }
  element('htCompile').addEventListener('click', () => handle(previewCurrent));
  element('htRun').addEventListener('click', () => handle(async () => {
    requireDocument(); scene?.stopPreview();
    if (requestedMode === 'live' && !liveCalibrationReady()) throw failure('CALIBRATION_REQUIRED', guided.four_point_required === true ? '请完成当前连接四点标定，并确认双轴已原位锁定且反馈有效' : '当前连接的四边界限位未验证，请先重新读取并保存');
    await saveGuidedDocument();
    element('htEditorState').textContent = '模板已保存 · 标定保持复用';
    runKey ||= scope.crypto?.randomUUID?.() || 'ui-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    const payload = await prepareGuidedRun(model, api, requestedMode, element('htLiveConfirmed').checked, runKey);
    if (jsonDirty || draftAreas.size) throw failure('STALE_PLAN', '预览期间草稿已变更');
    renderPlan(); const result = await api('guided/run', payload); if (result.state) renderState(result);
    runKey = null; element('htLiveConfirmed').checked = false; await refresh();
  }));
  element('htMode').addEventListener('change', () => { requestedMode = element('htMode').value; element('htLiveConfirmed').checked = false; model?.invalidate(); runKey = null; updateControls(); });
  element('htLiveConfirmed').addEventListener('change', updateControls);
  element('htStop').addEventListener('click', async () => { cancelDefaultsFeedback(); scene?.stopPreview(); keyboardSessionStopped = true; void keyboardInput.stop('global'); updateControls(); try { await api('stop', {}); await refresh(); } catch (error) { showError(error); } });
  for (const [id, route] of [['htPause', 'pause'], ['htResume', 'resume']]) element(id).addEventListener('click', () => handle(async () => { await api(route, {}); await refresh(); }));
  element('htAnimate').addEventListener('click', () => { if (model?.plan && !physicalAttached()) scene?.preview(model.plan.steps, model.plan.experiment?.defaults || model.document.defaults); });
  for (const [id, view] of [['htCameraAll', 'all'], ['htCameraPlate', 'plate'], ['htCameraBoards', 'boards']]) element(id).addEventListener('click', () => scene?.cameraView(view));
  function showCalibration(visible) { if (!visible) void keyboardInput.stop('collapse'); element('htCalibrationPanel').hidden = !visible; element('htCalibrationToggle').setAttribute('aria-expanded', String(visible)); updateControls(); }
  element('htCalibrationToggle').addEventListener('click', () => { showCalibration(element('htCalibrationPanel').hidden); });
  element('htCalibrationHide').addEventListener('click', () => showCalibration(false));
  function showLimits(visible) { element('htLimitsPanel').hidden = !visible; element('htLimitsToggle').setAttribute('aria-expanded', String(visible)); }
  element('htLimitsToggle').addEventListener('click', () => showLimits(element('htLimitsPanel').hidden));
  element('htLimitsHide').addEventListener('click', () => showLimits(false));
  element('htLimitsClearance').addEventListener('change', updateControls);
  element('htTwinDatumCorner').addEventListener('change', () => { twinDatumSelected = true; element('htTwinDatumConfirm').checked = false; renderTwinRegistration(); updateControls(); });
  element('htTwinDatumConfirm').addEventListener('change', updateControls);
  element('htTwinRegisterDatum').addEventListener('click', () => handle(async () => {
    if (!canRegisterTwinDatum()) throw failure('TWIN_DATUM_REQUIRED', '请先读取当前连接所选的两个边界，并确认双轴反馈有效');
    if (!element('htTwinDatumConfirm').checked) throw failure('TWIN_CONFIRMATION_REQUIRED', '请确认所选边界与已读取记录对应，并接受名义CAD端点显示假设');
    const datum = element('htTwinDatumCorner').value;
    twinRevision++; twinAction = true;
    try { twin = await api('twin/register-datum', {datum, confirmed: true}); twinReceivedAt = Date.now(); twinReadError = null; element('htTwinDatumConfirm').checked = false; renderTwin(); }
    catch (error) { twinReadError = error; renderTwin(); throw error; }
    finally { twinAction = false; }
  }));
  for (const [, id] of twinFields) element(id).addEventListener('input', () => { element('htTwinConfirm').checked = false; updateControls(); });
  element('htTwinConfirm').addEventListener('change', updateControls);
  element('htTwinRegister').addEventListener('click', () => handle(async () => {
    if (!limitsActive() || snapshot.busy || guided.manual?.active) throw failure('TWIN_LIMITS_REQUIRED', '请先读取并保存当前连接的四边界限位');
    if (!element('htTwinConfirm').checked) throw failure('TWIN_CONFIRMATION_REQUIRED', '请确认四个值是各实测边界对应的 CAD 坐标');
    const cad_bounds = Object.fromEntries(twinFields.map(([key, id]) => [key, numeric(id)]));
    twinRevision++; twinAction = true;
    try { twin = await api('twin/register', {cad_bounds, confirmed: true}); twinReceivedAt = Date.now(); twinReadError = null; element('htTwinConfirm').checked = false; renderTwin(); }
    catch (error) { twinReadError = error; renderTwin(); throw error; }
    finally { twinAction = false; }
  }));
  element('htLimitsBegin').addEventListener('click', () => handle(async () => {
    if (stateUnavailable || limitsUnavailable || snapshot.busy || guided.manual?.active || limitCalibrationActive() || !snapshot.motion?.connected) throw failure('LIMITS_UNAVAILABLE', '请先确认轴已连接且其他实验与标定已结束');
    showLimits(true); await changeLimits('begin', {});
  }));
  for (const [boundary] of LIMIT_BOUNDARIES) element('htLimitCapture-' + boundary).addEventListener('click', () => handle(async () => {
    if (limitsUnavailable || !limits.calibrating) throw failure('LIMITS_NOT_CALIBRATING', '请先主动开始读取四个边界');
    await changeLimits('capture', {boundary});
  }));
  element('htLimitsSave').addEventListener('click', () => handle(async () => {
    if (limitsUnavailable || !limits.calibrating || !limitsComplete()) throw failure('LIMITS_INCOMPLETE', '请先完整读取四个边界');
    if (!element('htLimitsClearance').checked) throw failure('LIMITS_CLEARANCE_REQUIRED', '请确认已留安全余量，未把机械硬挡作为运行边界');
    await changeLimits('save', {clearance_confirmed: true});
  }));
  element('htLimitsCancel').addEventListener('click', () => handle(async () => {
    if (!limitCalibrationActive()) throw failure('LIMITS_NOT_CALIBRATING', '当前未在读取四边界限位');
    await changeLimits('cancel', {});
  }));
  for (const id of ['htCalibrationMotionConfirmed', 'htCalibrationReleaseConfirmed', 'htCalibrationZSupported']) element(id).addEventListener('change', updateControls);
  function calibrationOperationContext() {
    return JSON.stringify([guided.manual?.session_token, guided.manual?.connection_token, snapshot.motion?.connection_token]);
  }
  for (const [id, route, payload] of [
    ['htCalibrationBegin', 'begin', {}], ['htCaptureFirst', 'capture', {point: 'first'}],
    ['htCaptureLast', 'capture', {point: 'last'}], ['htCaptureWorking', 'capture', {point: 'working'}], ['htCaptureSafe', 'capture', {point: 'safe'}],
    ['htCalibrationFinish', 'finish', {}], ['htCalibrationCancel', 'cancel', {}],
  ]) element(id).addEventListener('click', () => {
    const stoppingJog = keyboardInput.stop(route);
    return handle(async () => {
    const operation = {route, point: payload.point, phase: 'pending', context: calibrationOperationContext()};
    calibrationOperation = operation; renderCalibration();
    try {
    await stoppingJog;
    requireDocument('calibration');
    if (route === 'begin') {
      if (keyboardMode() ? !element('htCalibrationMotionConfirmed').checked
        : !element('htCalibrationReleaseConfirmed').checked || !element('htCalibrationZSupported').checked) {
        throw failure('CALIBRATION_CONFIRMATION_REQUIRED', keyboardMode() ? '请先确认电极、孔板姿态与两轴运动余量，允许本次键盘标定运动' : '请先确认允许带电松轴、持续供电，且 Z 已可靠支撑不会下滑');
      }
      if (stateUnavailable || !keyboardConnectionAvailable()) throw failure('MOTION_DISCONNECTED', '请先在高级设置选择并保存 X / Z 总线端口');
      if (keyboardMode() && (snapshot.recovery_required || (guided.manual?.active && !keyboardRestartAvailable()))) throw failure('CALIBRATION_RECOVERY_REQUIRED', '请先使用停止按钮完成双轴停止复核，再明确重新开始标定');
      if (!guided.manual?.active) await saveGuidedDocument();
    } else {
      if (!guided.manual?.active) throw failure('CALIBRATION_NOT_ACTIVE', '请先明确开始本次手动标定');
      if (route !== 'cancel' && keyboardMode() && (!keyboardControllerAvailable() || !keyboardSessionOwned())) throw failure('CALIBRATION_RECOVERY_REQUIRED', '请确认停止，并重新进入拥有双轴锁定反馈的键盘标定');
    }
    if (route === 'finish' && !calibrationPointsComplete()) throw failure('CALIBRATION_POINTS_REQUIRED', '请分别读取第 1 / 12 列 X、浸入工作 Z 与提出安全 Z');
    calibrationRevision++;
      const next = await api('guided/calibration/' + route, route === 'begin' ? keyboardMode()
        ? {control_mode: 'keyboard', motion_confirmed: true, ...(guided.manual?.boundary_at_current ? {boundary_at_current: guided.manual.boundary_at_current} : {})}
        : {release_confirmed: true, z_supported: true} : payload);
      if (calibrationOperation !== operation) return;
      guided = next; operation.phase = 'success'; operation.context = calibrationOperationContext();
      if (route === 'finish') syncFinishedTravel(next);
      if (route === 'begin') { jogOrigin = null; jogNotice = ''; jogSettingsDirty = false; keyboardSessionStopped = false; }
      renderCalibration();
      await refresh();
    } catch (error) {
      if (calibrationOperation === operation) {
        operation.phase = 'error'; operation.error = error; operation.points = JSON.stringify(guided.manual?.points || {});
        renderCalibration();
      }
      await Promise.allSettled([refreshGuided(), refresh()]);
      throw error;
    }
  }); });
  for (const id of ['htJogSpeed', 'htJogMaxSpeed']) {
    element(id).addEventListener('input', () => {
      void keyboardInput.stop('settings'); jogSettingsDirty = true;
      element('htJogSpeed').max = element('htJogMaxSpeed').value;
      element('htJogFastClearance').hidden = !(Number(element('htJogMaxSpeed').value) > 2); updateControls();
    });
    element(id).addEventListener('focus', () => { void keyboardInput.stop('focus'); });
  }
  element('htJogApply').addEventListener('click', () => {
    const stoppingJog = keyboardInput.stop('settings');
    return handle(async () => {
      await stoppingJog;
      if (!keyboardMode() || !guided.manual?.active) throw failure('CALIBRATION_NOT_ACTIVE', '请先进入键盘标定');
      if (!keyboardControllerAvailable() || !keyboardSessionOwned()) throw failure('CALIBRATION_RECOVERY_REQUIRED', '请确认停止，并重新进入拥有双轴锁定反馈的键盘标定');
      const settings = validateJogSettings(numeric('htJogSpeed'), numeric('htJogMaxSpeed'), element('htJogFastConfirmed').checked);
      calibrationRevision++;
      guided = await api('guided/calibration/jog/config', {session_token: guided.manual.session_token, ...settings});
      jogSettingsDirty = false; renderCalibration();
    });
  });
  element('htJogStop').addEventListener('click', () => { void keyboardInput.stop('stop'); });
  for (const [id, speed, maximum] of [['htJogFine', 0.5, 1], ['htJogFast', 20, 20]]) {
    element(id).addEventListener('click', () => {
      void keyboardInput.stop('settings');
      element('htJogSpeed').value = String(speed); element('htJogMaxSpeed').value = String(maximum);
      element('htJogSpeed').max = String(maximum);
      element('htJogFastConfirmed').checked = false; jogSettingsDirty = true;
      element('htJogFastClearance').hidden = maximum <= 2; updateControls();
    });
  }
  element('htJogFastConfirmed').addEventListener('change', () => { void keyboardInput.stop('settings'); jogSettingsDirty = true; });
  for (const [id, direction] of [['htJogUp', 'w'], ['htJogLeft', 'a'], ['htJogRight', 'd'], ['htJogDown', 's']]) {
    element(id).addEventListener('pointerdown', event => {
      if (event.button !== 0 || element(id).disabled || jogSettingsDirty) return;
      event.preventDefault(); element(id).setPointerCapture?.(event.pointerId);
      keyboardInput.pointerdown(String(event.pointerId), direction);
    });
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) element(id).addEventListener(type, event => keyboardInput.pointerup(String(event.pointerId)));
    element(id).addEventListener('contextmenu', event => event.preventDefault());
  }
  scope.addEventListener?.('keydown', event => { if (!jogSettingsDirty) keyboardInput.keydown(event); renderFrontendUpdate(); });
  scope.addEventListener?.('keyup', event => { keyboardInput.keyup(event); renderFrontendUpdate(); });
  scope.addEventListener?.('pointerup', event => keyboardInput.pointerup(String(event.pointerId)));
  for (const type of ['blur', 'pagehide']) scope.addEventListener?.(type, () => { void keyboardInput.stop(type); });
  document.addEventListener?.('visibilitychange', () => { if (document.hidden || document.visibilityState === 'hidden') void keyboardInput.stop('hidden'); });
  document.addEventListener?.('focusin', () => { void keyboardInput.stop('focus'); });
  document.addEventListener?.('compositionstart', () => { void keyboardInput.stop('composition'); });
  if (typeof scope.MutationObserver === 'function') new scope.MutationObserver(() => keyboardInput.sync()).observe(root, {attributes: true, attributeFilter: ['class', 'hidden']});
  element('htAdvanced').addEventListener('toggle', () => { if (model) model.advancedOpen = element('htAdvanced').open; });
  element('htPorts').addEventListener('click', () => handle(async () => {
    const response = await api('ports'); element('htPortOptions').replaceChildren();
    for (const port of Array.isArray(response) ? response : response.ports || []) {
      const option = node('option'); option.value = typeof port === 'string' ? port : port.device || port.port; option.textContent = port.description || option.value; element('htPortOptions').append(option);
    }
  }));
  element('htMotionConnect').addEventListener('click', () => handle(async () => { requireDocument(); await api('motion/connect', {motion: model.document.bindings.motion}); await refresh(); }));
  element('htMotionReference').addEventListener('click', () => handle(async () => {
    if (!element('htReferenceConfirmed').checked) throw failure('REFERENCE_CONFIRMATION_REQUIRED', '请先人工确认参考点');
    await api('motion/reference', {confirmed: true}); await refresh();
  }));
  for (const axis of ['X', 'Z']) element('htEnable' + axis).addEventListener('click', () => handle(async () => {
    if (!element('htEnableConfirmed').checked) throw failure('ENABLE_CONFIRMATION_REQUIRED', '请先明确允许轴使能');
    await api('motion/enable', {axis}); await refresh();
  }));
  element('htMotionStop').addEventListener('click', async () => { cancelDefaultsFeedback(); keyboardSessionStopped = true; void keyboardInput.stop('global'); updateControls(); try { await api('motion/stop', {}); await refresh(); } catch (error) { showError(error); } });
  element('htMotionDisconnect').addEventListener('click', () => handle(async () => { await api('motion/disconnect', {}); await refresh(); }));
  const draftGroups = {
    defaults: ['htOutput', ...movementFields.map(([key]) => 'htDefault-' + key), ...CHANNELS.map(id => 'htElectrode-' + id)],
    bindings: ['htPort', 'htBaud', 'htMotionPolicy', 'htElectricalPolicy', ...['A', 'B'].flatMap(slot => ['htBoardAddress-' + slot, 'htBoardSettings-' + slot]), ...['X', 'Z'].flatMap(axis => axisFields.map(key => 'htAxis-' + axis + '-' + key))],
  };
  for (const [area, ids] of Object.entries(draftGroups)) for (const id of ids) element(id).addEventListener('input', () => {
    if (area === 'defaults') dirtyDefaultsFeedback();
    if (id.startsWith('htBoardAddress-')) boardAddressDrafts.add(id.slice('htBoardAddress-'.length));
    draftAreas.add(area); model?.invalidate(); runKey = null; element('htLiveConfirmed').checked = false; scene?.stopPreview(); element('htEditorState').textContent = '高级表单草稿待应用'; updateControls();
  });
  element('htDefaultDuration').addEventListener('input', dirtyDefaultsFeedback);
  renderDefaultsFeedback();
  async function resetDevices() {
    if (pending) return false;
    pending = true; keyboardSessionStopped = true;
    void keyboardInput.stop('reset'); scene?.stopPreview();
    element('htError').hidden = true; element('htErrorDetails').hidden = true;
    element('htStatus').textContent = '正在停止任务并重新连接设备…'; updateControls();
    try {
      const currentGuided = await api('guided');
      const boundary = currentGuided.manual?.boundary_at_current;
      const payload = boundary ? {boundary_at_current: boundary} : {};
      const motion = devicesCache?.snapshot?.motion || snapshot.motion || {};
      const previousPort = motion.port || currentGuided.document?.bindings?.motion?.port;
      if (motion.connected === false && previousPort) {
        const enumerated = await api('ports'), ports = Array.isArray(enumerated) ? enumerated : enumerated.ports || [];
        if (!ports.some(port => (port.device || port.port || port) === previousPort)) {
          const selected = element('htPort').value.trim();
          const candidates = ports.filter(port => /USB|usbserial|wchusbserial/i.test([port.device, port.description, port.hwid].join(' ')));
          const replacement = ports.find(port => (port.device || port.port || port) === selected)
            || (candidates.length === 1 ? candidates[0] : null);
          const replacementPort = replacement && (replacement.device || replacement.port || replacement);
          if (!replacementPort) throw failure('motion_port_unavailable', '旧串口 ' + previousPort + ' 已不存在。请在高级设置中枚举并选择当前 USB–RS485 串口，再点击「重连并重置」。');
          if (typeof scope.confirm !== 'function' || !scope.confirm('旧串口 ' + previousPort + ' 已不存在。\n当前选择：' + replacementPort + '\n请确认这是原来连接 X/Z 双轴的同一个 RS485 转接器。确认后仅停止并读取，不会移动；旧四点标定作废。')) {
            element('htStatus').textContent = '已取消更换串口，未启动实验或标定';
            return false;
          }
          payload.port = replacementPort; payload.same_bus_confirmed = true;
        }
      }
      const result = await api('motion/reset', payload);
      snapshot = result.state; guided = result.guided; stateUnavailable = false;
      const receivedAt = Date.now(), devices = await api('devices');
      devicesCache = {snapshot: devices, available: true, error: null, received_at_ms: receivedAt};
      scope.PAHighThroughputDevices = devicesCache;
      scope.renderHighThroughputDevices?.(devices);
      calibrationRevision++; jogOrigin = null; jogNotice = ''; jogSettingsDirty = false;
      keyboardSessionStopped = false; model?.invalidate(); runKey = null;
      element('htCalibrationMotionConfirmed').checked = false;
      await refresh(); renderCalibration();
      scope.toast?.(result.message || '设备已重新连接，请重新进入标定');
      return true;
    } catch (error) {
      showError(error); scope.toast?.(errorGuidance(error, guided.manual, snapshot));
      await refresh(); return false;
    } finally { pending = false; updateControls(); }
  }
  element('htResetDevices').addEventListener('click', () => { void resetDevices(); });
  scope.HighThroughput = {
    resetDevices,
    activate() { void createScene(); requestAnimationFrame(() => scene?.resize()); void refreshTwin(); void refresh(); void refreshBoards(); },
    deactivate() { cancelDefaultsFeedback(); void keyboardInput.stop('tab'); },
    acceptDevices(next) { devicesCache = next; renderTwin(); renderCalibration(); },
  };
  updateControls();
  void refreshFrontendVersion();
  setInterval(() => { void refreshFrontendVersion(); }, 5000);
  setInterval(() => { void refreshBoards(); if (root.classList.contains('active')) void refreshTwin(); if (root.classList.contains('active') || snapshot.busy) void refresh(); }, 1100);
})(typeof window === 'undefined' ? globalThis : window);
