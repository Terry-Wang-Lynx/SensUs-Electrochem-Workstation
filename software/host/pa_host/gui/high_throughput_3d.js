(function (scope) {
  'use strict';
  function previewTarget(step, defaults, current) {
    const target = {...current}, params = step.resolved_params || {};
    if (step.op === 'move_x') target.x = -Number(params.target_mm);
    if (step.op === 'move_z') target.z = -(Number(params.target_mm) - Number(defaults.z_safe_mm || 0));
    return target;
  }

  function buildApparatus(Three, boardData, cadData) {
    if (cadData?.schema_version !== '1.0' || cadData.units !== 'mm') throw new Error('整机 CAD 网格版本或单位不受支持');
    const group = new Three.Group(), fixedGroup = new Three.Group(), xGroup = new Three.Group(), zGroup = new Three.Group();
    group.add(fixedGroup, xGroup, zGroup);
    const motionGroups = {fixed: fixedGroup, x: xGroup, z: zGroup};
    for (const [name, motionGroup] of Object.entries(motionGroups)) motionGroup.name = 'motion-' + name;
    const palette = {pcb: 0x1b7763, metal: 0x9cabb0, shell: 0xe4e4dc, chip: 0x27363b, ceramic: 0xbda75f, orange: 0xe88b42, blue: 0x4c87b5, electrode: 0xad8265, wireA: 0x31988d, wireB: 0x8f78b4, base: 0xd4dddc, rail: 0x627980, well: 0xd8e8e8};
    const materials = {};
    for (const [key, color] of Object.entries(palette)) materials[key] = new Three.MeshStandardMaterial({color, roughness: key === 'metal' ? 0.3 : 0.65, metalness: key === 'metal' ? 0.45 : 0.05});
    const mesh = (parent, geometry, material) => {
      const result = new Three.Mesh(geometry, typeof material === 'string' ? materials[material] : material);
      parent.add(result); return result;
    };
    const box = (parent, x, y, z, width, depth, height, material) => {
      const result = mesh(parent, new Three.BoxGeometry(width, depth, height), material);
      result.position.set(x, y, z); return result;
    };
    const cylinder = (parent, x, y, z, radius, height, material) => {
      const result = mesh(parent, new Three.CylinderGeometry(radius, radius, height, 32), material);
      result.rotation.x = Math.PI / 2; result.position.set(x, y, z); return result;
    };
    const cadGeometry = source => {
      const geometry = new Three.BufferGeometry();
      geometry.setAttribute('position', new Three.Float32BufferAttribute(source.vertices.flat(), 3));
      geometry.setIndex(source.faces.flat()); geometry.computeVertexNormals(); return geometry;
    };
    const parts = [];
    for (const part of cadData.parts) {
      const parent = motionGroups[part.motion];
      if (!parent || !materials[part.kind]) throw new Error('CAD 零件运动归属或材质未定义：' + part.name);
      const rendered = mesh(parent, cadGeometry(part), part.kind);
      rendered.name = part.name;
      rendered.userData = {source: part.source, solid_index: part.solid_index, translation_mm: part.translation_mm, motion: part.motion};
      parts.push(rendered);
    }
    group.userData = {sources: cadData.sources, referencePose: cadData.reference_pose};
    const boards = [], wires = [], geometries = boardData.board.map(part => ({...part, geometry: cadGeometry(part)}));
    for (let boardIndex = 0; boardIndex < 2; boardIndex++) {
      const card = new Three.Group();
      card.position.set(-32, boardIndex === 0 ? -37 : 9, 132);
      card.quaternion.setFromRotationMatrix(new Three.Matrix4().makeBasis(new Three.Vector3(0, 1, 0), new Three.Vector3(0, 0, -1), new Three.Vector3(-1, 0, 0)));
      zGroup.add(card);
      for (const part of geometries) mesh(card, part.geometry, part.kind);
      card.userData.cadMeshCount = geometries.length; boards.push(card);
      cylinder(card, -0.05, -2.594, -4.1, 12.25, 5, 'metal');
      const ports = [-11.65, -2.75, 5.95, 14.45];
      for (let channel = 0; channel < 4; channel++) {
        box(card, ports[channel], -13, 3.65, 6.7, 6.6, 4.2, 'shell');
        const port = new Three.Vector3(ports[channel], -16.3, 3.65);
        card.localToWorld(port);
        const row = -49.25 + (boardIndex * 4 + channel) * 9;
        const end = new Three.Vector3(0, row + 4.6, 147.8);
        const curve = new Three.CatmullRomCurve3([port, new Three.Vector3(port.x, port.y, 158), new Three.Vector3(-18, (port.y + end.y) / 2, 170 + channel), new Three.Vector3(0, end.y, 160), end]);
        const wire = mesh(zGroup, new Three.TubeGeometry(curve, 35, 0.42, 6, false), boardIndex ? 'wireB' : 'wireA');
        wire.name = 'ABCDEFGH'[boardIndex * 4 + channel] + '_Wire_REFERENCE';
        wire.userData.electrode = 'ABCDEFGH'[boardIndex * 4 + channel]; wires.push(wire);
      }
    }
    const wells = [], columns = [];
    for (let columnIndex = 1; columnIndex <= 12; columnIndex++) {
      const columnWells = cadData.wells.filter(well => well.column === columnIndex);
      if (columnWells.length !== 8) throw new Error('CAD 孔板列必须包含 8 个孔：' + columnIndex);
      const first = columnWells[0], rowMinimum = Math.min(...columnWells.map(well => well.center[1])), rowMaximum = Math.max(...columnWells.map(well => well.center[1]));
      const material = new Three.MeshStandardMaterial({color: 0x6dbbb1, transparent: true, opacity: 0.22, roughness: 0.45, depthWrite: false});
      const column = box(xGroup, first.center[0], (rowMinimum + rowMaximum) / 2, first.center[2] + 0.25, 8.4, rowMaximum - rowMinimum + 8.4, 0.5, material);
      column.userData = {column: columnIndex, role: 'selection_overlay'}; columns.push(column);
      for (const source of columnWells) {
        const well = new Three.Group(); well.position.set(...source.center);
        xGroup.add(well); wells.push(well);
        const liquid = mesh(well, new Three.CircleGeometry(source.radius_mm * 0.92, 32), new Three.MeshStandardMaterial({color: 0x72bdb2, transparent: true, opacity: 0.75, side: Three.DoubleSide}));
        liquid.position.z = source.floor_z + (source.center[2] - source.floor_z) * 0.65 - source.center[2];
        well.userData = {column: columnIndex, row: source.row, liquid};
      }
    }
    let physicalAttached = false;
    function setPreviewPose(pose) {
      if (physicalAttached) return false;
      xGroup.position.x = pose.x;
      zGroup.position.z = pose.z;
      return true;
    }
    function clearFeedbackPose() {
      xGroup.position.x = 0; zGroup.position.z = 0;
    }
    function setPhysicalAttached(attached) {
      if (!!attached !== physicalAttached) clearFeedbackPose();
      physicalAttached = !!attached;
    }
    function setFeedbackPose(pose) {
      if (!physicalAttached || pose?.source !== 'encoder_feedback' || !Number.isFinite(pose.scene_x_mm) || !Number.isFinite(pose.scene_z_mm)
        || !['X', 'Z'].every(axis => Number.isFinite(pose.feedback_age_s?.[axis]) && pose.feedback_age_s[axis] >= 0 && pose.feedback_age_s[axis] <= 2)) return false;
      xGroup.position.x = pose.scene_x_mm; zGroup.position.z = pose.scene_z_mm;
      return true;
    }
    function setElectrodes(electrodes) {
      const enabled = new Set(electrodes.filter(electrode => electrode.enabled).map(electrode => electrode.id));
      for (const part of parts) if (/^[A-H]_Electrode_REFERENCE$/.test(part.name)) part.visible = enabled.has(part.name[0]);
      for (const wire of wires) wire.visible = enabled.has(wire.userData.electrode);
    }
    return {group, fixedGroup, xGroup, zGroup, parts, boards, wires, columns, wells, setPreviewPose, setElectrodes, setPhysicalAttached, setFeedbackPose, clearFeedbackPose};
  }

  async function create(stage, options = {}) {
    const Three = scope.THREE;
    if (!Three) throw new Error('本地 Three.js 未加载');
    const [boardResponse, apparatusResponse] = await Promise.all([scope.fetch('/assets/ht-geometry.json'), scope.fetch('/assets/ht-apparatus.json')]);
    if (!boardResponse.ok || !apparatusResponse.ok) throw new Error('CAD 网格读取失败');
    const [boardData, cadData] = await Promise.all([boardResponse.json(), apparatusResponse.json()]);
    const scene = new Three.Scene();
    scene.background = new Three.Color(0xeef3f2);
    const assembly = buildApparatus(Three, boardData, cadData); scene.add(assembly.group);
    const renderer = new Three.WebGLRenderer({antialias: true, alpha: false});
    renderer.setPixelRatio(Math.min(scope.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = Three.SRGBColorSpace;
    stage.prepend(renderer.domElement);
    scene.add(new Three.HemisphereLight(0xffffff, 0xa0b3b0, 2.7));
    for (const position of [[-180, -200, 350], [240, 100, 220]]) {
      const light = new Three.DirectionalLight(0xffffff, 2.2); light.position.set(...position); scene.add(light);
    }
    const camera = new Three.PerspectiveCamera(37, 1, 0.1, 2000); camera.up.set(0, 0, 1);
    const target = new Three.Vector3(2, 12, 88);
    let azimuth = -1.95, elevation = 0.58, distance = 565, selectedColumn = 1, animation = null;
    let pointer = null, hidden = false, physicalAttached = false;
    const labels = [], columnLabels = [], raycaster = new Three.Raycaster();
    const label = (text, position, parent, className = '') => {
      const element = document.createElement('span'); element.textContent = text; element.className = 'ht-scene-label ' + className;
      options.labels?.append(element);
      const item = {element, position: new Three.Vector3(...position), parent}; labels.push(item); return item;
    };
    label('X · 移动孔板 / 地址 1', [105, -15, 38], assembly.fixedGroup);
    label('Z · 固定背架 / 地址 2', [0, 65, 205], assembly.fixedGroup);
    label('板 A · A–D', [-46, -37, 132], assembly.zGroup);
    label('板 B · E–H', [-46, 9, 132], assembly.zGroup);
    for (let column = 1; column <= 12; column++) {
      const well = cadData.wells.find(well => well.column === column && well.row === 'A');
      columnLabels.push(label(String(column), [well.center[0], well.center[1] - 10, well.center[2] + 1], assembly.xGroup, 'ht-column-label'));
    }
    for (const row of ['A', 'H']) {
      const well = cadData.wells.find(well => well.column === 1 && well.row === row);
      label(row, [well.center[0] - 10, well.center[1], well.center[2] + 1], assembly.xGroup, 'ht-row-label');
    }
    function draw() {
      if (hidden || stage.clientWidth < 1 || stage.clientHeight < 1) return;
      camera.position.copy(target).add(new Three.Vector3(Math.cos(azimuth) * Math.cos(elevation), Math.sin(azimuth) * Math.cos(elevation), Math.sin(elevation)).multiplyScalar(distance * Math.max(1, 0.9 / camera.aspect)));
      camera.lookAt(target); camera.updateMatrixWorld(); scene.updateMatrixWorld(true); renderer.render(scene, camera);
      for (const item of labels) {
        const point = item.parent.localToWorld(item.position.clone()).project(camera);
        item.element.hidden = point.z > 1 || Math.abs(point.x) > 0.96 || Math.abs(point.y) > 0.96;
        item.element.style.left = (point.x + 1) * stage.clientWidth / 2 + 'px';
        item.element.style.top = (1 - point.y) * stage.clientHeight / 2 + 'px';
      }
    }
    function resize() {
      const width = stage.clientWidth, height = stage.clientHeight;
      if (width < 1 || height < 1) { hidden = true; return; }
      hidden = false; renderer.setSize(width, height, false); camera.aspect = width / height; camera.updateProjectionMatrix(); draw();
    }
    function selectColumn(index) {
      selectedColumn = index;
      for (const column of assembly.columns) {
        column.material.opacity = column.userData.column === index ? 0.40 : 0.07;
        column.material.color.setHex(column.userData.column === index ? 0x208c7c : 0x79bcb4);
      }
      columnLabels.forEach((item, offset) => item.element.classList.toggle('selected', offset + 1 === index)); draw();
    }
    function setColumns(columns) {
      for (const well of assembly.wells) {
        const column = columns.find(item => item.index === well.userData.column);
        const concentration = Number(column?.concentration?.value || 0);
        well.userData.liquid.material.color.setHSL(0.46 - Math.min(0.18, Math.log10(1 + concentration) * 0.06), 0.36, 0.57);
        well.userData.liquid.visible = !!column && column.enabled !== false;
      }
      for (const [offset, item] of columnLabels.entries()) {
        const column = columns.find(column => column.index === offset + 1);
        item.element.title = column ? (column.name ?? column.solution ?? '') : '未定义';
        item.element.classList.toggle('unused', column?.enabled === false);
      }
      selectColumn(selectedColumn);
    }
    function cameraView(view) {
      if (view === 'plate') { target.set(49.5 + assembly.xGroup.position.x, -15, 67); azimuth = -1.48; elevation = 1.02; distance = 260; }
      else if (view === 'boards') { target.set(-24, -13, 132 + assembly.zGroup.position.z); azimuth = -2.82; elevation = 0.3; distance = 205; }
      else { target.set(2, 12, 88); azimuth = -1.95; elevation = 0.58; distance = 565; }
      draw();
    }
    function stopPreview() {
      if (animation !== null) cancelAnimationFrame(animation);
      animation = null;
      if (!physicalAttached) {
        assembly.setPreviewPose({x: 0, z: 0});
        options.onPreview?.('CAD 基准抬升 35 mm · 非实物位置 / 未标定浸入深度');
      }
      draw();
    }
    function setPhysicalAttached(attached) {
      if (!!attached === physicalAttached) return;
      stopPreview(); physicalAttached = !!attached; assembly.setPhysicalAttached(physicalAttached); draw();
    }
    function setFeedbackPose(pose) {
      setPhysicalAttached(true); stopPreview();
      const accepted = assembly.setFeedbackPose(pose); draw(); return accepted;
    }
    function clearFeedbackPose() {
      stopPreview(); assembly.clearFeedbackPose(); draw();
    }
    function preview(steps, defaults = {}) {
      if (physicalAttached) return false;
      stopPreview();
      let cursor = 0, started = null, fromX = 0, fromZ = 0;
      function frame(now) {
        if (physicalAttached) { animation = null; return; }
        if (cursor >= steps.length) { animation = null; options.onPreview?.('计划示意结束 · 非实物位置'); return; }
        const step = steps[cursor];
        if (started === null) { started = now; fromX = assembly.xGroup.position.x; fromZ = assembly.zGroup.position.z; }
        const {x: toX, z: toZ} = previewTarget(step, defaults, {x: fromX, z: fromZ});
        const ratio = Math.min(1, (now - started) / 650), eased = ratio * ratio * (3 - 2 * ratio);
        assembly.setPreviewPose({x: fromX + (toX - fromX) * eased, z: fromZ + (toZ - fromZ) * eased});
        options.onPreview?.(`计划示意 · 第 ${cursor + 1}/${steps.length} 步 ${step.op} · 非实物位置 / 非实际时长`);
        draw();
        if (ratio === 1) { cursor++; started = null; }
        animation = requestAnimationFrame(frame);
      }
      animation = requestAnimationFrame(frame);
    }
    stage.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      pointer = {id: event.pointerId, x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, moved: false};
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener('pointermove', event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      if (Math.hypot(event.clientX - pointer.startX, event.clientY - pointer.startY) > 5) pointer.moved = true;
      if (pointer.moved) {
        azimuth -= (event.clientX - pointer.x) * 0.008;
        elevation = Math.max(0.08, Math.min(1.48, elevation + (event.clientY - pointer.y) * 0.008)); draw();
      }
      pointer.x = event.clientX; pointer.y = event.clientY;
    });
    stage.addEventListener('pointerup', event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      if (!pointer.moved) {
        const bounds = renderer.domElement.getBoundingClientRect();
        raycaster.setFromCamera(new Three.Vector2((event.clientX - bounds.left) / bounds.width * 2 - 1, 1 - (event.clientY - bounds.top) / bounds.height * 2), camera);
        const picked = raycaster.intersectObjects(assembly.columns, false)[0];
        if (picked) options.onSelectColumn?.(picked.object.userData.column);
      }
      pointer = null;
    });
    stage.addEventListener('pointercancel', () => { pointer = null; });
    stage.addEventListener('wheel', event => {
      event.preventDefault(); distance = Math.max(110, Math.min(700, distance * Math.exp(event.deltaY * 0.001))); draw();
    }, {passive: false});
    new ResizeObserver(resize).observe(stage);
    selectColumn(1); resize();
    function setElectrodes(electrodes) { assembly.setElectrodes(electrodes); draw(); }
    return {resize, selectColumn, setColumns, setElectrodes, cameraView, preview, stopPreview, setPhysicalAttached, setFeedbackPose, clearFeedbackPose};
  }
  const api = {buildApparatus, create, previewTarget};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  scope.HighThroughputScene = api;
})(typeof window === 'undefined' ? globalThis : window);
