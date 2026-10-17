import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

// Photo-inspired approximation. Each complete drawer has one movable parent node.
export function createCabinet(container, onSelect) {
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(33, 1, 0.1, 100);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.6));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;
  renderer.domElement.setAttribute('aria-label', '可拖动查看的三层抽屉柜简化模型');
  renderer.domElement.setAttribute('role', 'img');
  container.append(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.enablePan = false;
  controls.minDistance = 8;
  controls.maxDistance = 23;
  controls.minPolarAngle = Math.PI * 0.19;
  controls.maxPolarAngle = Math.PI * 0.51;
  controls.target.set(0, 2.65, 0.25);

  const root = new THREE.Group();
  scene.add(root);
  const material = (color, roughness = 0.65, metalness = 0.03) => new THREE.MeshStandardMaterial({ color, roughness, metalness });
  const materials = {
    frame: material('#e6e7e7', 0.44),
    rail: material('#b8bdc3', 0.3, 0.65),
    bin: material('#7d858c', 0.7),
    front: material('#aeb6bc', 0.47),
    inset: material('#b9c0c5', 0.5),
    handle: material('#555b61', 0.22, 0.7),
    wheel: material('#393c40', 0.85),
    hub: material('#747a80', 0.5, 0.3),
    item: material('#c4cedb', 0.65),
    paper: material('#eceeec', 0.8),
    band: material('#6682aa', 0.65),
    bottle: material('#d6d8d2', 0.58),
    cable: material('#4c5663', 0.65),
  };
  const geometries = new Map();
  function box(parent, dimensions, position, mat, radius = 0.035) {
    const key = dimensions.join(',') + ':' + radius;
    if (!geometries.has(key)) geometries.set(key, new RoundedBoxGeometry(...dimensions, 2, radius));
    const mesh = new THREE.Mesh(geometries.get(key), mat);
    mesh.position.set(...position);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }
  function cylinder(parent, radius, height, position, mat, rotation = null) {
    const key = `cylinder:${radius}:${height}`;
    if (!geometries.has(key)) geometries.set(key, new THREE.CylinderGeometry(radius, radius, height, 16));
    const mesh = new THREE.Mesh(geometries.get(key), mat);
    mesh.position.set(...position);
    if (rotation) mesh.rotation.set(...rotation);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }

  // W:H:D approximately 3.3:5.6:2.35, including the top rail and casters.
  for (const y of [0.57, 1.94, 3.31, 4.68]) {
    box(root, [3.3, 0.15, 2.38], [0, y, 0], materials.frame, 0.065);
  }
  for (const x of [-1.48, 1.48]) {
    for (const z of [-1.02, 1.02]) {
      cylinder(root, 0.065, 4.05, [x, 2.62, z], materials.rail);
      cylinder(root, 0.105, 0.15, [x, 0.44, z], materials.rail);
      box(root, [0.19, 0.23, 0.2], [x, 0.3, z], materials.hub, 0.045);
      cylinder(root, 0.22, 0.15, [x, 0.22, z], materials.wheel, [0, 0, Math.PI / 2]);
    }
  }
  for (const x of [-1.3, 1.3]) box(root, [0.15, 0.53, 0.16], [x, 5.0, -0.95], materials.frame, 0.06);
  box(root, [2.75, 0.16, 0.18], [0, 5.24, -0.95], materials.frame, 0.065);
  box(root, [3.12, 0.04, 2.18], [0, 4.78, 0], materials.inset, 0.015);

  const drawers = [];
  const fronts = [];
  const contents = [];
  for (let i = 0; i < 3; i++) {
    const drawer = new THREE.Group();
    drawer.name = `drawer_${i + 1}`;
    drawer.position.y = 3.99 - i * 1.37;
    drawer.userData.drawer = i + 1;
    root.add(drawer);
    box(drawer, [2.82, 0.08, 2.05], [0, -0.54, 0], materials.bin);
    for (const x of [-1.38, 1.38]) box(drawer, [0.075, 1.1, 2.03], [x, 0, 0], materials.bin);
    box(drawer, [2.78, 1.1, 0.075], [0, 0, -1.0], materials.bin);
    const faceMaterial = materials.front.clone();
    const front = box(drawer, [2.88, 1.12, 0.11], [0, 0, 1.045], faceMaterial, 0.055);
    box(drawer, [2.62, 0.89, 0.035], [0, 0, 1.112], materials.inset, 0.035);
    // Small circular pulls echo the reference cabinet without transparent materials.
    cylinder(drawer, 0.067, 0.09, [0, 0.02, 1.18], materials.rail, [Math.PI / 2, 0, 0]);
    cylinder(drawer, 0.105, 0.045, [0, 0.02, 1.23], materials.handle, [Math.PI / 2, 0, 0]);
    for (const mesh of drawer.children) mesh.userData.drawer = i + 1;
    drawers.push(drawer);
    fronts.push(front);
    const content = new THREE.Group();
    content.userData.drawer = i + 1;
    drawer.add(content);
    contents.push(content);
  }

  function itemModel(kind) {
    const group = new THREE.Group();
    if (kind === 'cable' || kind === 'headphones') {
      const key = 'cable-coil';
      if (!geometries.has(key)) geometries.set(key, new THREE.TorusGeometry(0.19, 0.024, 6, 32));
      for (let i = 0; i < 3; i++) {
        const ring = new THREE.Mesh(geometries.get(key), materials.cable);
        ring.rotation.x = Math.PI / 2;
        ring.position.y = 0.035 + i * 0.04;
        ring.castShadow = true;
        group.add(ring);
      }
      box(group, [0.12, 0.09, 0.12], [0.24, 0.05, 0.1], materials.paper, 0.014);
      box(group, [0.06, 0.05, 0.08], [0.24, 0.05, 0.19], materials.handle, 0.01);
    } else if (kind === 'pill' || kind === 'milk') {
      cylinder(group, 0.17, 0.39, [0, 0.195, 0], materials.bottle);
      cylinder(group, 0.171, 0.15, [0, 0.19, 0], materials.band);
      cylinder(group, 0.125, 0.08, [0, 0.43, 0], materials.paper);
    } else {
      const snack = kind === 'cookie' || kind === 'coffee';
      box(group, [0.65, 0.25, 0.4], [0, 0.125, 0], snack ? materials.item : materials.paper, 0.035);
      box(group, [0.18, 0.256, 0.405], [0.08, 0.125, 0], materials.band, 0.018);
      if (snack) for (const x of [-0.31, 0.31]) box(group, [0.04, 0.16, 0.38], [x, 0.12, 0], materials.paper, 0.012);
    }
    return group;
  }
  const inventory = new Map();
  const slotPosition = (index) => new THREE.Vector3([-0.8, 0, 0.8][index % 3], -0.48, index < 3 ? 0.58 : -0.32);
  function syncInventory(items) {
    const visibleItems = [1, 2, 3].flatMap((drawer) => items.filter((item) => item.drawer === drawer).slice(0, 6));
    const visibleIds = new Set(visibleItems.map((item) => item.id));
    for (const [id, entry] of inventory) {
      if (!visibleIds.has(id)) { entry.mesh.removeFromParent(); inventory.delete(id); }
    }
    for (let drawer = 1; drawer <= 3; drawer++) {
      visibleItems.filter((item) => item.drawer === drawer).forEach((item, index) => {
        let entry = inventory.get(item.id);
        if (entry && entry.kind !== item.kind) { entry.mesh.removeFromParent(); inventory.delete(item.id); entry = null; }
        if (!entry) {
          entry = { mesh: itemModel(item.kind), kind: item.kind };
          inventory.set(item.id, entry);
        }
        contents[drawer - 1].add(entry.mesh);
        entry.mesh.position.copy(slotPosition(index));
        entry.mesh.visible = !(transfer?.item.id === item.id && transfer.direction === 'in');
      });
    }
  }
  const transferLayer = new THREE.Group();
  root.add(transferLayer);

  scene.add(new THREE.HemisphereLight('#ffffff', '#9da3ad', 2.5));
  const keyLight = new THREE.DirectionalLight('#ffffff', 3.4);
  keyLight.position.set(-3, 8, 5);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(1024, 1024);
  keyLight.shadow.camera.left = -7;
  keyLight.shadow.camera.right = 7;
  keyLight.shadow.camera.top = 8;
  keyLight.shadow.camera.bottom = -6;
  keyLight.shadow.normalBias = 0.035;
  keyLight.shadow.bias = -0.0001;
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight('#dfe9ff', 1.3);
  fillLight.position.set(5, 3, -3);
  scene.add(fillLight);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: 0.13 }));
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.015;
  floor.receiveShadow = true;
  scene.add(floor);

  let selected = 1;
  let dark = false;
  let targets = [0, 0, 0];
  let animation = 0;
  let previousTime = 0;
  let reducedMotion = false;
  let contextLost = false;
  let transfer = null;
  let inventorySignature = '';
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let pointerDown = null;

  function render() {
    if (!document.hidden && !contextLost) renderer.render(scene, camera);
  }
  const easeInOut = (value) => value < 0.5 ? 4 * value ** 3 : 1 - (-2 * value + 2) ** 3 / 2;
  function clearTransfer() {
    if (!transfer) return;
    transfer.mesh.removeFromParent();
    const stored = inventory.get(transfer.item.id);
    if (stored) stored.mesh.visible = true;
    transfer = null;
    invalidate();
  }
  function updateTransfer(delta) {
    if (!transfer) return false;
    const current = transfer;
    current.elapsed += delta;
    const ready = Math.abs(targets[current.drawer - 1] - drawers[current.drawer - 1].position.z) < 0.025;
    if (!current.started && (ready || reducedMotion)) {
      current.started = true;
      current.elapsed = 0;
      current.mesh.visible = !reducedMotion;
    }
    if (!current.started) return true;
    const progress = reducedMotion ? 1 : Math.min(current.elapsed / 1.35, 1);
    const travel = easeInOut(progress);
    const drawer = drawers[current.drawer - 1];
    const slot = current.slot;
    const inside = new THREE.Vector3(slot.x, drawer.position.y + slot.y, drawer.position.z + slot.z);
    const outside = new THREE.Vector3(0.85, drawer.position.y + 0.5, drawer.position.z + 2.6);
    const path = new THREE.CubicBezierCurve3(outside,
      new THREE.Vector3(0.6, drawer.position.y + 0.9, drawer.position.z + 1.8),
      new THREE.Vector3(slot.x, drawer.position.y + 0.95, drawer.position.z + slot.z), inside);
    const distance = current.direction === 'in' ? travel : 1 - travel;
    current.mesh.position.copy(path.getPoint(distance));
    current.mesh.rotation.y = (1 - distance) * -0.16;
    const appearance = current.direction === 'in' ? Math.min(progress / 0.1, 1) : Math.min((1 - progress) / 0.12, 1);
    current.mesh.scale.setScalar(Math.max(0.001, appearance) * (1 + Math.sin(distance * Math.PI) * 0.16));
    const projected = current.mesh.position.clone();
    projected.y += 0.2;
    projected.project(camera);
    current.onFrame?.({ progress, x: (projected.x + 1) / 2 * container.clientWidth, y: (1 - projected.y) / 2 * container.clientHeight });
    if (progress >= 1) {
      const complete = current.onComplete;
      clearTransfer();
      complete?.();
      return false;
    }
    return true;
  }
  function animate(time) {
    animation = 0;
    if (document.hidden || contextLost) return;
    const delta = Math.min((time - previousTime) / 1000 || 0.016, 0.05);
    previousTime = time;
    let moving = false;
    drawers.forEach((drawer, i) => {
      const error = targets[i] - drawer.position.z;
      if (Math.abs(error) > 0.001 && !reducedMotion) {
        drawer.position.z += error * (1 - Math.exp(-12 * delta));
        moving = true;
      } else drawer.position.z = targets[i];
    });
    moving = updateTransfer(delta) || moving;
    render();
    if (moving) animation = requestAnimationFrame(animate);
  }
  function invalidate() {
    if (!animation && !document.hidden && !contextLost) {
      previousTime = performance.now();
      animation = requestAnimationFrame(animate);
    }
  }
  function resetView() {
    const aspect = Math.max(container.clientWidth / Math.max(container.clientHeight, 1), 0.4);
    const factor = aspect < 0.85 ? 1.2 : 1;
    camera.position.set(7.9 * factor, 6.2 * factor, 11 * factor);
    controls.target.set(0, 2.65, 0.25);
    controls.update();
    invalidate();
  }
  function resize() {
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height) return;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    invalidate();
  }
  controls.addEventListener('change', invalidate);
  new ResizeObserver(resize).observe(container);
  renderer.domElement.addEventListener('pointerdown', (event) => { pointerDown = [event.clientX, event.clientY]; });
  renderer.domElement.addEventListener('pointerup', (event) => {
    if (!pointerDown || Math.hypot(event.clientX - pointerDown[0], event.clientY - pointerDown[1]) > 5) return;
    const bounds = renderer.domElement.getBoundingClientRect();
    pointer.set((event.clientX - bounds.left) / bounds.width * 2 - 1, -(event.clientY - bounds.top) / bounds.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(root.children, true).find((result) => result.object.userData.drawer);
    if (hit) onSelect(hit.object.userData.drawer);
  });
  renderer.domElement.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();
    contextLost = true;
    const complete = transfer?.onComplete;
    clearTransfer();
    complete?.();
    document.getElementById('model-fallback').hidden = false;
  });
  renderer.domElement.addEventListener('webglcontextrestored', () => {
    contextLost = false;
    document.getElementById('model-fallback').hidden = true;
    invalidate();
  });
  document.addEventListener('visibilitychange', invalidate);
  resize();
  resetView();

  return {
    resetView,
    update({ open, items = [], drawer = selected, theme = dark ? 'dark' : 'light', reduced = false, instant = false }) {
      selected = drawer;
      dark = theme === 'dark';
      reducedMotion = reduced || matchMedia('(prefers-reduced-motion: reduce)').matches;
      targets = open.map((value) => value ? 1.3 : 0);
      if (instant) drawers.forEach((mesh, i) => { mesh.position.z = targets[i]; });
      const signature = JSON.stringify(items.map((item) => [item.id, item.drawer, item.kind]));
      if (signature !== inventorySignature) { syncInventory(items); inventorySignature = signature; }
      scene.background = new THREE.Color(dark ? '#1b1c1e' : '#f2f2f2');
      fronts.forEach((front, i) => {
        front.material.emissive.set(i + 1 === selected ? (dark ? '#33415b' : '#233047') : '#000000');
        front.material.emissiveIntensity = i + 1 === selected ? 0.12 : 0;
      });
      invalidate();
    },
    playTransfer({ direction, item, drawer, onFrame, onComplete }) {
      clearTransfer();
      if (contextLost) { onComplete?.(); return; }
      const mesh = itemModel(item.kind);
      mesh.visible = false;
      transferLayer.add(mesh);
      const stored = inventory.get(item.id);
      const slot = stored?.mesh.position.clone() || slotPosition(0);
      // Rear-row contents use the visible tray mouth for the illustrative handoff.
      slot.z = 0.58;
      if (stored) stored.mesh.visible = false;
      transfer = { direction, item, drawer, mesh, slot, onFrame, onComplete, elapsed: 0, started: false };
      invalidate();
    },
    cancelTransfer: clearTransfer,
    snapshot() {
      return { drawerPositions: drawers.map((drawer) => drawer.position.z), renderedFrames: renderer.info.render.frame, triangles: renderer.info.render.triangles, visibleItems: inventory.size, itemTransfer: transfer ? { direction: transfer.direction, drawer: transfer.drawer, item: transfer.item.name, progress: transfer.started ? Math.min(transfer.elapsed / 1.35, 1) : 0, position: transfer.mesh.position.toArray() } : null };
    },
  };
}
