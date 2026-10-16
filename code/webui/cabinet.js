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
    const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, height, 16), mat);
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
  }

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
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let pointerDown = null;

  function render() {
    if (!document.hidden && !contextLost) renderer.render(scene, camera);
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
    update({ open, drawer = selected, theme = dark ? 'dark' : 'light', reduced = false, instant = false }) {
      selected = drawer;
      dark = theme === 'dark';
      reducedMotion = reduced || matchMedia('(prefers-reduced-motion: reduce)').matches;
      targets = open.map((value) => value ? 1.3 : 0);
      if (instant) drawers.forEach((mesh, i) => { mesh.position.z = targets[i]; });
      scene.background = new THREE.Color(dark ? '#1b1c1e' : '#f2f2f2');
      fronts.forEach((front, i) => {
        front.material.emissive.set(i + 1 === selected ? (dark ? '#33415b' : '#233047') : '#000000');
        front.material.emissiveIntensity = i + 1 === selected ? 0.12 : 0;
      });
      invalidate();
    },
    snapshot() {
      return { drawerPositions: drawers.map((drawer) => drawer.position.z), renderedFrames: renderer.info.render.frame, triangles: renderer.info.render.triangles };
    },
  };
}
