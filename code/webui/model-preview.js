import './style.css';
import './model-preview.css';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { createIcons, Box, Moon, Sun, ArrowLeft, RotateCcw, Scan, PanelRightClose, PanelRightOpen, Download, Check, X } from 'lucide';

const icons = { Box, Moon, Sun, ArrowLeft, RotateCcw, Scan, PanelRightClose, PanelRightOpen, Download, Check, X };
const $ = (id) => document.getElementById(id);
const icon = (name) => `<i data-lucide="${name}"></i>`;
const paintIcons = () => createIcons({ icons, attrs: { 'aria-hidden': 'true' } });
const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const mobile = matchMedia('(max-width: 760px)');
const container = $('model-viewport');
let dark = false;
let model;
let renderer;
let pendingFrame = 0;
let activeModel;
let parts = [];
let version = 0;
let frameCount = 0;
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, 1, 0.05, 100);
let controls;
const material = new THREE.MeshStandardMaterial({ color: '#b5bdc3', roughness: 0.7, metalness: 0.04, side: THREE.DoubleSide });

function draw() {
  pendingFrame = 0;
  if (!renderer || document.hidden) return;
  renderer.render(scene, camera);
  frameCount++;
}
function invalidate() { if (!pendingFrame && renderer && !document.hidden) pendingFrame = requestAnimationFrame(draw); }
function resize() {
  if (!renderer || !container.clientWidth || !container.clientHeight) return;
  camera.aspect = container.clientWidth / container.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(container.clientWidth, container.clientHeight, false);
  invalidate();
}
function resetView() {
  const factor = container.clientWidth / container.clientHeight < 0.85 ? 1.3 : 1;
  camera.position.set(7.6 * factor, 5.3 * factor, 10.7 * factor);
  controls?.target.set(0, 2.65, 0);
  controls?.update();
  invalidate();
}
function setTheme() {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  scene.background = new THREE.Color(dark ? '#1b1c1e' : '#f2f2f2');
  $('theme-button').innerHTML = icon(dark ? 'sun' : 'moon');
  document.querySelector('meta[name="theme-color"]').content = dark ? '#1b1c1e' : '#f2f2f2';
  paintIcons();
  invalidate();
}
function setPanel(collapsed) {
  $('model-info').hidden = collapsed;
  document.querySelector('.model-workspace').classList.toggle('info-collapsed', collapsed);
  $('panel-toggle').innerHTML = icon(collapsed ? 'panel-right-open' : 'panel-right-close');
  $('panel-toggle').setAttribute('aria-expanded', String(!collapsed));
  $('panel-toggle').setAttribute('aria-label', collapsed ? '展开模型信息' : '收起模型信息');
  paintIcons();
}
function release(group) {
  const geometries = new Set();
  group.traverse((object) => { if (object.geometry) geometries.add(object.geometry); });
  geometries.forEach((geometry) => geometry.dispose());
  group.removeFromParent();
}
async function loadModel(entry) {
  const requestVersion = ++version;
  $('model-status').textContent = '正在载入模型';
  try {
    const loaded = await new GLTFLoader().loadAsync(entry.file);
    if (requestVersion !== version) { release(loaded.scene); return; }
    if (model) release(model);
    model = loaded.scene;
    model.rotation.y = entry.rotationY || 0;
    model.updateMatrixWorld(true);
    const bounds = new THREE.Box3().setFromObject(model);
    const size = bounds.getSize(new THREE.Vector3());
    if (!size.y || !Number.isFinite(size.y)) throw new Error('Invalid model bounds');
    const center = bounds.getCenter(new THREE.Vector3());
    const scale = 5.4 / size.y;
    model.position.set(-center.x * scale, -bounds.min.y * scale, -center.z * scale);
    model.scale.setScalar(scale);
    const replacedMaterials = new Set();
    let triangles = 0;
    let meshes = 0;
    parts = [];
    model.traverse((object) => {
      if (!object.isMesh) return;
      for (const old of (Array.isArray(object.material) ? object.material : [object.material])) if (old) replacedMaterials.add(old);
      object.material = material;
      object.castShadow = true;
      object.receiveShadow = true;
      triangles += (object.geometry.index?.count ?? object.geometry.attributes.position.count) / 3;
      meshes++;
      parts.push(object);
    });
    replacedMaterials.forEach((old) => old.dispose());
    scene.add(model);
    activeModel = { ...entry, triangles, meshes, size: size.toArray() };
    $('parts-section').hidden = parts.length <= 1;
    $('part-select').innerHTML = '<option value="all">全部部件</option>' + parts.map((part, index) => `<option value="${index}">部件 ${String(index + 1).padStart(2, '0')}</option>`).join('');
    $('face-count').textContent = Math.round(triangles).toLocaleString('zh-CN');
    $('file-size').textContent = entry.bytes >= 1048576 ? (entry.bytes / 1048576).toFixed(2) + ' MB' : Math.round(entry.bytes / 1024) + ' KB';
    $('model-caption').textContent = entry.caption;
    $('model-note').textContent = entry.note;
    $('model-status').textContent = entry.status;
    $('download-model').href = entry.file;
    document.querySelectorAll('[data-model]').forEach((button) => {
      const selected = button.dataset.model === entry.id;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.querySelector('.selected-mark').toggleAttribute('hidden', !selected);
    });
    resetView();
    if (mobile.matches) setPanel(true);
  } catch {
    $('model-status').textContent = '模型暂时无法载入';
  }
}

async function start() {
  const response = await fetch('/models/catalog.json');
  if (!response.ok) throw new Error('Catalog unavailable');
  const catalog = await response.json();
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.6));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;
  renderer.domElement.setAttribute('role', 'img');
  renderer.domElement.setAttribute('aria-label', '可旋转查看的 AI 生成柜体');
  container.append(renderer.domElement);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enablePan = false;
  controls.enableDamping = false;
  controls.minDistance = 7;
  controls.maxDistance = 24;
  controls.maxPolarAngle = Math.PI * 0.53;
  controls.addEventListener('change', invalidate);
  scene.add(new THREE.HemisphereLight('#ffffff', '#a7adb5', 2.5));
  const light = new THREE.DirectionalLight('#ffffff', 3);
  light.position.set(-3, 8, 5);
  light.castShadow = true;
  light.shadow.mapSize.set(1024, 1024);
  light.shadow.camera.left = -7; light.shadow.camera.right = 7;
  light.shadow.camera.top = 8; light.shadow.camera.bottom = -6;
  light.shadow.normalBias = 0.035;
  scene.add(light);
  const fill = new THREE.DirectionalLight('#e5eaf0', 1.2);
  fill.position.set(5, 3, -3); scene.add(fill);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: 0.13 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = -0.015; floor.receiveShadow = true; scene.add(floor);
  $('model-options').innerHTML = catalog.models.map((entry) => `<button class="model-option" data-model="${escape(entry.id)}" aria-pressed="false">${icon('box')}<span><strong>${escape(entry.name)}</strong><small>${escape(entry.subtitle)}</small></span><i class="selected-mark" data-lucide="check" hidden></i></button>`).join('');
  $('reference-images').innerHTML = catalog.references.map((entry, i) => `<button class="reference-image" data-reference="${i}" aria-label="查看${escape(entry.label)}参考照片"><img src="${escape(entry.file)}" alt="${escape(entry.label)}柜体参考照片" loading="lazy"></button>`).join('');
  $('model-options').addEventListener('click', (event) => {
    const button = event.target.closest('[data-model]');
    const entry = catalog.models.find((item) => item.id === button?.dataset.model);
    if (entry) loadModel(entry);
  });
  $('reference-images').addEventListener('click', (event) => {
    const button = event.target.closest('[data-reference]');
    if (!button) return;
    $('reference-large').src = catalog.references[Number(button.dataset.reference)].file;
    $('reference-dialog').showModal();
  });
  renderer.domElement.addEventListener('webglcontextlost', (event) => { event.preventDefault(); $('model-status').textContent = '3D 画面暂不可用'; });
  renderer.domElement.addEventListener('webglcontextrestored', () => { $('model-status').textContent = activeModel?.status || ''; invalidate(); });
  new ResizeObserver(resize).observe(container);
  setTheme(); resize();
  await loadModel(catalog.models[0]);
}
$('theme-button').addEventListener('click', () => { dark = !dark; setTheme(); });
$('reset-view').addEventListener('click', resetView);
$('panel-toggle').addEventListener('click', () => setPanel(!$('model-info').hidden));
$('wireframe').addEventListener('click', () => { material.wireframe = !material.wireframe; $('wireframe').setAttribute('aria-pressed', String(material.wireframe)); invalidate(); });
$('close-reference').addEventListener('click', () => $('reference-dialog').close());
$('part-select').addEventListener('change', (event) => {
  parts.forEach((part, index) => { part.visible = event.target.value === 'all' || Number(event.target.value) === index; });
  invalidate();
});
mobile.addEventListener('change', () => setPanel(mobile.matches));
document.addEventListener('visibilitychange', invalidate);
setPanel(mobile.matches);
start().catch(() => { $('model-status').textContent = '模型预览暂时无法载入'; });
window.mindBoxModelPreview = { snapshot: () => ({ model: activeModel, frames: frameCount, triangles: renderer?.info.render.triangles, dark, wireframe: material.wireframe, parts: parts.map((part) => ({ name: part.name, visible: part.visible, triangles: (part.geometry.index?.count ?? part.geometry.attributes.position.count) / 3, bounds: new THREE.Box3().setFromObject(part).getSize(new THREE.Vector3()).toArray() })) }) };
