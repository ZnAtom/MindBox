import './style.css';
import { createIcons, Box, Moon, Sun, Settings2, RotateCcw, Play, Archive, Plus, PanelRightClose, PanelRightOpen, Check, CircleDashed, Mic, Square, HardDrive, MessageCircle, X, ArrowUp, ChevronRight, ChevronDown, Sparkles, RefreshCw, ArrowDownLeft, ArrowUpRight, CameraOff, PackagePlus, PackageMinus, Search, Cable, Cookie, Pill, Milk, Coffee, CupSoda, Package, Headphones, BatteryCharging, Smartphone, NotebookPen, Monitor, Trash2, Bell, CheckCheck, History, Volume2 } from 'lucide';
import { samplePhotos } from './item-visuals.js';

const $ = (id) => document.getElementById(id);
const icons = { Box, Moon, Sun, Settings2, RotateCcw, Play, Archive, Plus, PanelRightClose, PanelRightOpen, Check, CircleDashed, Mic, Square, HardDrive, MessageCircle, X, ArrowUp, ChevronRight, ChevronDown, Sparkles, RefreshCw, ArrowDownLeft, ArrowUpRight, CameraOff, PackagePlus, PackageMinus, Search, Cable, Cookie, Pill, Milk, Coffee, CupSoda, Package, Headphones, BatteryCharging, Smartphone, NotebookPen, Monitor, Trash2, Bell, CheckCheck, History, Volume2 };
const iconNames = new Set(Object.keys(icons).map((name) => name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase()));
const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;
const paintIcons = () => createIcons({ icons, attrs: { 'aria-hidden': 'true' } });
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
function itemVisual(item, size = 'thumb') {
  const photo = item.photo;
  const symbol = iconNames.has(item.kind) ? item.kind : 'package';
  return `<span class="item-visual item-visual--${size} ${photo?.mode === 'cutout' ? 'is-cutout' : ''}"><span class="photo-fallback">${icon(symbol)}</span>${photo?.thumbnailUrl ? `<img data-item-photo src="${esc(photo.thumbnailUrl)}" alt="" width="480" height="480" decoding="async" ${size === 'thumb' ? 'loading="lazy"' : ''}>` : ''}</span>`;
}
const storageKey = 'mind-box-preview-v2';
const drawerNames = ['零食与补给', '日常用品', '数码与随身'];
const ordinals = ['第一层', '第二层', '第三层'];
const systemDark = matchMedia('(prefers-color-scheme: dark)');
const mobile = matchMedia('(max-width: 760px)');
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
const uid = () => crypto.randomUUID();

function localDate(offset = 0) {
  const date = new Date();
  date.setDate(date.getDate() + offset);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
const timeNow = () => new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
const monthDay = (date) => date ? date.slice(5).replace('-', '/') : '未填写日期';
function dateLabel(date) { return date === localDate() ? '今天' : date === localDate(1) ? '明天' : monthDay(date); }
function makeItem(id, name, drawer, kind, days, expiry = '', note = '', voice = '') {
  return { id, name, drawer, kind, storedOn: localDate(-days), expiry, note, voice };
}
function photoFixtures() {
  return [
    { ...makeItem('sample-cola', '可乐', 1, 'cup-soda', 2), shape: 'can', photo: samplePhotos.cola },
    { ...makeItem('sample-chicken', '鸡胸肉', 1, 'package', 1, '', '保存方式与保质期以包装为准。'), shape: 'packet', photo: samplePhotos.chicken },
    { ...makeItem('sample-tea', '瓶装茶', 1, 'milk', 3), shape: 'bottle', photo: samplePhotos.tea },
  ];
}
function fixtures() {
  return {
    version: 2, photoSamplesVersion: 1, theme: 'light', suggestions: true, reducedMotion: false,
    devices: [
      { id: 'mb-01', code: '01', name: '宿舍收纳柜', open: [false, false, false], drawerNames: [...drawerNames], items: [
        ...photoFixtures(),
        makeItem('nuts', '混合坚果', 1, 'cookie', 6, localDate(12)),
        makeItem('biscuit', '燕麦饼干', 1, 'cookie', 4, localDate(30)),
        makeItem('seaweed', '海苔', 1, 'package', 2, localDate(60)),
        makeItem('coffee', '挂耳咖啡', 1, 'coffee', 12, localDate(120)),
        makeItem('fish-oil', '鱼油', 2, 'pill', 12, localDate(180), '每日补给'),
        makeItem('detergent', '洗衣液', 2, 'milk', 3),
        makeItem('wipes', '湿巾', 2, 'package', 5),
        makeItem('tissue', '纸巾', 2, 'package', 1),
        makeItem('cable', '充电线', 3, 'cable', 8, '', '白色 USB-C 充电线'),
        makeItem('charger', '充电头', 3, 'smartphone', 7),
        makeItem('earphones', '耳机', 3, 'headphones', 2),
        makeItem('powerbank', '充电宝', 3, 'battery-charging', 14),
      ], reminders: [
        { id: 'fruit', title: '把水果吃了', time: '20:00', date: localDate(), repeat: 'once', done: false },
        { id: 'daily-oil', title: '吃鱼油', time: '09:00', date: localDate(1), repeat: 'daily', done: false },
      ], activity: [{ title: '放入纸巾', drawer: 2, time: '14:32', direction: 'in' }, { title: '放入充电线', drawer: 3, time: '昨天', direction: 'in' }] },
      { id: 'mb-02', code: '02', name: '书桌收纳柜', open: [false, false, false], drawerNames: ['文具', '随身物品', '备用物品'], items: [makeItem('notebook', '笔记本', 1, 'notebook-pen', 3), makeItem('desk-cable', '备用充电线', 3, 'cable', 10)], reminders: [], activity: [] },
    ],
  };
}

// The physical cabinet is populated only by the inventory service.
let state = fixtures();
try {
  const saved = JSON.parse(localStorage.getItem(storageKey));
  if (saved?.version === 2 && Array.isArray(saved.devices) && saved.devices.length && saved.devices.every((device) => typeof device.id === 'string' && typeof device.name === 'string' && Array.isArray(device.items) && Array.isArray(device.reminders) && Array.isArray(device.activity) && device.open?.length === 3 && device.drawerNames?.length === 3)) {
    state = { ...state, ...saved, photoSamplesVersion: saved.photoSamplesVersion || 0 };
  }
} catch { /* An unavailable or damaged local preview store must not block the UI. */ }
// Add the new samples once while preserving existing edits and later removals.
const needsPhotoSamples = state.photoSamplesVersion !== 1;
if (needsPhotoSamples) {
  const device = state.devices.find((entry) => entry.id === 'mb-01');
  if (device) device.items.unshift(...photoFixtures().filter((item) => !device.items.some((entry) => entry.id === item.id)));
  state.photoSamplesVersion = 1;
}
let activeId = state.devices[0].id;
let selectedDrawer = 1;
let activeTab = 'overview';
let drawerFilter = 0;
let query = '';
let suggestionIndex = 0;
let panelCollapsed = mobile.matches;
let toastTimer;
let cabinet;
let storageWarningShown = false;
let transferFeedback = null;
let receiptTimer;
let demoPhotoIndex = 0;
let hardware = { connected: false, state: null };
let camera = { connected: false, latest: null, cached_frames: 0 };
let audio = { enabled: false, status: 'disabled' };
let vision = { enabled: false, status: 'disabled' };
let voiceQuery = { recorder: null, stream: null, chunks: [], recording: false, busy: false };
let wake = { enabled: false, status: 'disabled', event: 0 };
const microphoneClient = 'web-' + crypto.randomUUID();
const speechJobs = new Set();
let lastSpokenAnswer = '';
let voiceInitialized = false;
const seenDoorResults = new Set();
const attemptedDeliveries = new Set();
let pendingDeliveries = [];
let reminderSignature = '';
let inventoryMatches = null;
let searchTimer;
// The first preview cabinet is the single physical cabinet in this prototype.
const hardwareDeviceId = 'mb-01';
const physicalDevice = state.devices.find((device) => device.id === hardwareDeviceId);
if (physicalDevice) Object.assign(physicalDevice, { items: [], activity: [], reminders: [] });
let inventory = { connected: false, revision: 0, known: false };
let recording = { counts: {}, sessions: [], capture: {} };
let inventorySignature = '';
let recordingSignature = '';
const isPhysical = () => activeDevice().id === hardwareDeviceId;

function persist() {
  try { localStorage.setItem(storageKey, JSON.stringify(state)); }
  catch {
    if (!storageWarningShown) { toast('浏览器未允许保存，当前更改仅在本页保留'); storageWarningShown = true; }
  }
}
function activeDevice() { return state.devices.find((device) => device.id === activeId) || state.devices[0]; }
function resolvedTheme() { return state.theme === 'system' ? (systemDark.matches ? 'dark' : 'light') : state.theme; }
function toast(text) {
  clearTimeout(toastTimer);
  $('toast').textContent = text;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3000);
}
function applyTheme() {
  const theme = resolvedTheme();
  document.documentElement.dataset.theme = theme;
  $('theme-button').innerHTML = icon(theme === 'dark' ? 'sun' : 'moon');
  document.querySelector('meta[name="theme-color"]').content = theme === 'dark' ? '#1b1c1e' : '#f2f2f2';
  syncModel();
  paintIcons();
}
function syncModel(instant = false) {
  cabinet?.update({ open: activeDevice().open, items: activeDevice().items, drawer: selectedDrawer, theme: resolvedTheme(), reduced: state.reducedMotion, instant });
}
function transferBusy() { return transferFeedback?.phase === 'moving'; }
function isNewRecord(id) { return Boolean(transferFeedback && id === transferFeedback.id); }
function syncTransferControls() {
  const busy = transferBusy();
  const linked = isPhysical();
  document.querySelectorAll('#demo-drawer, [data-action="demo-drawer"]').forEach((button) => { button.disabled = busy || linked; });
  document.querySelectorAll('#demo-insert, [data-action="demo-insert"]').forEach((button) => { button.disabled = busy || isPhysical(); });
  document.querySelectorAll('#remove-item, #item-form button[type="submit"]').forEach((button) => { button.disabled = busy || (isPhysical() && !inventory.connected); });
  const hasItems = activeDevice().items.some((item) => item.drawer === selectedDrawer);
  document.querySelectorAll('#demo-remove, [data-action="demo-remove"]').forEach((button) => { button.disabled = busy || !hasItems || isPhysical(); });
  document.documentElement.classList.toggle('reduce-motion', state.reducedMotion || motionPreference.matches);
}
function setPanel(collapsed, rememberFocus = false) {
  panelCollapsed = collapsed;
  $('workspace').classList.toggle('panel-collapsed', collapsed);
  $('side-panel').hidden = collapsed;
  $('expand-panel').hidden = !collapsed;
  $('expand-panel').setAttribute('aria-expanded', String(!collapsed));
  $('collapse-panel').setAttribute('aria-expanded', String(!collapsed));
  if (!collapsed) document.querySelector('.activity-dot').hidden = true;
  if (rememberFocus) (collapsed ? $('expand-panel') : $('collapse-panel')).focus();
}
function selectDrawer(drawer) {
  selectedDrawer = drawer;
  renderDock();
  syncModel();
}
function drawerKnown(device, index) {
  return device.id !== hardwareDeviceId || (hardware.connected && (!vision.enabled || hardware.state?.known?.[index] === true));
}
function drawerLabel(device, index) {
  return !drawerKnown(device, index) ? '待确认' : device.open[index] ? '已打开' : '已合上';
}
function renderDock() {
  const device = activeDevice();
  $('drawer-dock').innerHTML = device.drawerNames.map((name, index) => `<button class="drawer-button ${selectedDrawer === index + 1 ? 'selected' : ''} ${device.open[index] && drawerKnown(device, index) ? 'open' : ''} ${transferFeedback?.drawer === index + 1 ? (transferBusy() ? 'transferring' : 'just-changed') : ''}" data-select-drawer="${index + 1}" aria-pressed="${selectedDrawer === index + 1}" aria-label="选择${ordinals[index]}：${esc(name)}"><span class="drawer-number">0${index + 1}</span><span><strong>${esc(name)}</strong><small>${drawerLabel(device, index)} · ${device.items.filter((item) => item.drawer === index + 1).length} 件</small></span></button>`).join('');
  const opened = device.open.flatMap((open, index) => open ? [ordinals[index]] : []);
  const uncertain = device.open.some((_, index) => !drawerKnown(device, index));
  $('scene-state').innerHTML = `<span class="status-dot ${opened.length ? 'active' : ''}"></span><span>${uncertain ? '抽屉状态待确认' : opened.length ? opened.join('、') + '已打开' : '抽屉已合上'}</span>`;
  $('scene-state').classList.remove('transferring', 'receipt');
  if (transferFeedback) {
    const verb = transferFeedback.direction === 'in' ? '放入' : '取出';
    const moving = transferBusy();
    $('scene-state').classList.add(moving ? 'transferring' : 'receipt');
    $('scene-state').innerHTML = `${icon(moving ? (transferFeedback.direction === 'in' ? 'arrow-down-left' : 'arrow-up-right') : 'check')}<span>${moving ? '正在' : '已'}${verb} ${esc(transferFeedback.name)} · ${ordinals[transferFeedback.drawer - 1]}</span>`;
  }
  syncTransferControls();
  paintIcons();
}
function renderDevices() {
  $('device-list').innerHTML = state.devices.map((device) => `<button class="device-row ${device.id === activeId ? 'selected' : ''}" data-device="${esc(device.id)}" aria-pressed="${device.id === activeId}"><span class="device-symbol">${icon('archive')}</span><span class="device-copy"><strong>${esc(device.name)}</strong><small>3 层抽屉 · ${device.items.length} 件物品</small></span>${device.id === activeId ? icon('check') : ''}</button>`).join('');
}
function reminderMeta(reminder) { return reminder.repeat === 'daily' ? '每天 · ' + reminder.time : dateLabel(reminder.date) + ' · ' + reminder.time; }
function reminderRow(reminder) {
  return `<div class="reminder-row"><div class="reminder-time">${esc(reminder.time)}</div><div class="row-copy"><strong>${esc(reminder.title)}</strong><small>${reminder.repeat === 'daily' ? '每天' : esc(dateLabel(reminder.date))}</small></div><button class="reminder-check" data-complete="${esc(reminder.id)}" aria-label="完成提醒：${esc(reminder.title)}">${icon('check')}</button></div>`;
}
function suggestion() {
  const device = activeDevice();
  const dated = device.items.filter((item) => item.expiry && item.storedOn).sort((a, b) => a.expiry.localeCompare(b.expiry));
  const candidate = dated[suggestionIndex % Math.max(dated.length, 1)];
  if (!candidate) return '';
  const days = Math.max(0, Math.floor((Date.now() - new Date(candidate.storedOn + 'T00:00:00').getTime()) / 86400000));
  return `<section class="panel-section"><div class="suggestion"><div class="suggestion-heading">${icon('sparkles')}一个小建议<button data-action="next-suggestion" aria-label="换一条建议" title="换一条建议">${icon('refresh-cw')}</button></div><p>${esc(candidate.name)}已经放了 ${days} 天，<br>下次可以先拿它。</p><small>${ordinals[candidate.drawer - 1]} · ${esc(candidate.name)} · 日期 ${monthDay(candidate.expiry)}</small></div></section>`;
}
function liveView() {
  const device = activeDevice();
  const opened = device.open.flatMap((open, index) => open ? [ordinals[index]] : []);
  if (!opened.length) return '';
  const liveTitle = device.open.some((value, index) => value && !drawerKnown(device, index)) ? '抽屉状态待确认' : opened.join('、') + '已打开';
  const latest = device.activity[0];
  const cameraView = camera.connected && hardware.connected && device.id === hardwareDeviceId
    ? '<img class="camera-live" src="/api/camera/mjpeg" alt="K230 实时摄像头画面">'
    : `<div class="camera-empty">${icon('camera-off')}<span>等待摄像头画面</span></div>`;
  return `<section class="live-view"><div class="live-heading"><span class="status-dot active"></span>${liveTitle}<div class="inline-actions"><button class="icon-button small" data-action="demo-insert" aria-label="演示放入一件物品" data-tooltip="演示放入物品">${icon('package-plus')}</button><button class="icon-button small" data-action="demo-remove" aria-label="演示取出一件物品" data-tooltip="演示取出物品">${icon('package-minus')}</button></div></div>${cameraView}${latest?.demo ? `<div class="live-event ${isNewRecord(latest.id) ? 'new-record' : ''}">${latest.photo ? itemVisual(latest, 'mini') : icon(latest.direction === 'in' ? 'arrow-down-left' : 'arrow-up-right')}<span>${esc(latest.title)} · 已更新预览</span><span class="change-count">${latest.direction === 'in' ? '+1' : '−1'} 件</span></div><p class="live-meta">${esc(latest.time)} · ${ordinals[latest.drawer - 1]}</p>` : ''}</section>`;
}
function recordingLabel() {
  if (!inventory.connected) return '库存服务未连接';
  if (recording.capture.state === 'capturing') return '正在记录收纳过程';
  const counts = recording.counts;
  if (counts.processing || counts.queued) return `正在识别 · ${(counts.processing || 0) + (counts.queued || 0)} 次`;
  if (counts.failed || counts.pending) return `待核对 · ${(counts.failed || 0) + (counts.pending || 0)} 次`;
  if (!recording.model_configured) return '识别服务未就绪';
  return '记录已同步';
}
function recordingStrip() {
  if (!isPhysical()) return '';
  return `<div class="recording-strip"><span><span class="status-dot ${inventory.connected ? 'active' : ''}"></span>${esc(recordingLabel())}</span><button class="icon-button small" data-action="recording" aria-label="查看自动记录" title="查看自动记录">${icon('history')}</button></div>`;
}
function renderOverview() {
  const device = activeDevice();
  const reminders = device.reminders.filter((reminder) => !reminder.done).slice(0, 2);
  return `${recordingStrip()}${liveView()}<section class="panel-section"><div class="section-title"><h3>抽屉</h3><span class="muted" style="font-size:10px">${isPhysical() ? '已记录' : '共'} ${device.items.length} 件物品</span></div>${device.drawerNames.map((name, index) => `<button class="drawer-row" data-view-drawer="${index + 1}"><span class="row-index">0${index + 1}</span><span class="row-copy"><strong>${esc(name)}</strong><small>${device.items.filter((item) => item.drawer === index + 1).length} 件物品</small></span><span class="row-state ${device.open[index] && drawerKnown(device, index) ? 'open' : ''}"><span class="status-dot"></span>${drawerLabel(device, index)}</span>${icon('chevron-right')}</button>`).join('')}</section>${state.suggestions ? suggestion() : ''}<section class="panel-section"><div class="section-title"><h3>接下来的提醒</h3><button class="text-action" data-action="view-reminders">全部</button></div>${reminders.length ? reminders.map(reminderRow).join('') : '<p class="field-note">暂无提醒</p>'}</section><section class="panel-section"><div class="section-title"><h3>最近记录</h3></div>${device.activity.length ? device.activity.slice(0, 3).map((event) => `<div class="activity-row ${isNewRecord(event.id) ? 'new-record' : ''}">${icon(event.direction === 'out' ? 'arrow-up-right' : 'arrow-down-left')}<span class="row-copy"><strong>${esc(event.title)}</strong><small>${ordinals[event.drawer - 1] || '位置待确认'}</small></span><span class="activity-time">${esc(event.time)}</span></div>`).join('') : '<p class="field-note">暂无记录</p>'}</section>`;
}
function itemRows() {
  const items = activeDevice().items.filter((item) => (!drawerFilter || item.drawer === drawerFilter) &&
    (isPhysical() && inventoryMatches !== null ? inventoryMatches.includes(item.id) : item.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));
  return items.length ? items.map((item) => `<div class="item-row ${transferFeedback?.itemId === item.id ? 'new-record' : ''}"><button class="item-action" data-item="${esc(item.id)}">${itemVisual(item)}<span class="row-copy"><strong>${esc(item.name)}</strong><small>${ordinals[item.drawer - 1] || '位置待确认'} · ${item.storedOn ? monthDay(item.storedOn) + ' 放入' : '放入时间未知'}${item.expiry ? `<br><span class="item-date">日期 ${monthDay(item.expiry)}</span>` : ''}</small></span>${icon('chevron-right')}</button></div>`).join('') : `<div class="empty-state">${icon('search')}${query ? '没有找到相关物品' : isPhysical() ? '暂无已确认记录' : '这里还没有物品'}</div>`;
}
function renderItems() {
  return `${recordingStrip()}${isPhysical() ? `<div class="section-title"><h3>已记录物品</h3><button class="icon-button small" data-action="manual-add" aria-label="补录物品" title="补录物品">${icon('plus')}</button></div>` : ''}<label class="search-field">${icon('search')}<input id="item-search" aria-label="搜索当前设备物品" placeholder="搜索物品" value="${esc(query)}" maxlength="80"></label><div class="filter-tabs" aria-label="按抽屉筛选">${['全部', '一层', '二层', '三层'].map((name, index) => `<button data-filter="${index}" class="${drawerFilter === index ? 'active' : ''}" aria-pressed="${drawerFilter === index}">${name}</button>`).join('')}</div><div id="item-results">${itemRows()}</div>`;
}
function renderReminders() {
  const reminders = activeDevice().reminders;
  function groupRows(list) {
    return list.map((reminder) => `<div class="reminder-detail-row ${reminder.done ? 'done' : ''}"><button class="reminder-check" data-complete="${esc(reminder.id)}" aria-label="${reminder.done ? '恢复' : '完成'}提醒：${esc(reminder.title)}">${reminder.done ? icon('check') : ''}</button><button class="reminder-edit" data-reminder="${esc(reminder.id)}"><span class="row-copy"><strong>${esc(reminder.title)}</strong><small>${esc(reminderMeta(reminder))}</small></span></button><button class="icon-button small" data-reminder="${esc(reminder.id)}" aria-label="编辑提醒：${esc(reminder.title)}">${icon('chevron-right')}</button></div>`).join('');
  }
  const pending = reminders.filter((reminder) => !reminder.done);
  const done = reminders.filter((reminder) => reminder.done);
  const deliveries = isPhysical() ? pendingDeliveries.filter((entry) => entry.channel === 'voice').map((entry) =>
    `<div class="reminder-row"><span class="row-copy"><strong>${esc(entry.message)}</strong><small>${entry.state === 'failed' ? '待手动播报' : '提醒已到时间'}</small></span><button class="icon-button small" data-play-reminder="${esc(entry.id)}" aria-label="播报提醒" title="播报提醒">${icon('play')}</button></div>`).join('') : '';
  return `<div class="section-title"><h3>提醒与任务</h3><button class="icon-button small" data-action="add-reminder" aria-label="添加提醒" data-tooltip="添加提醒">${icon('plus')}</button></div>${deliveries}${pending.length ? groupRows(pending) : `<div class="empty-state">${icon('bell')}暂无待办提醒</div>`}${done.length ? `<div class="reminder-group-label">已结束</div>${groupRows(done)}` : ''}`;
}
function renderPanel() {
  const content = $('panel-content');
  content.innerHTML = activeTab === 'overview' ? renderOverview() : activeTab === 'items' ? renderItems() : renderReminders();
  content.setAttribute('aria-labelledby', 'tab-' + activeTab);
  document.querySelectorAll('[data-tab]').forEach((button) => {
    const selected = button.dataset.tab === activeTab;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  paintIcons();
  syncTransferControls();
}
function render(instant = false) {
  const device = activeDevice();
  $('scene-title').textContent = device.name;
  $('device-code').textContent = 'MIND BOX / ' + device.code;
  $('scene-subtitle').textContent = `3 个抽屉 · ${device.items.length} 件物品`;
  $('item-count').textContent = device.items.length;
  renderDevices(); renderDock(); renderPanel(); syncModel(instant);
  renderHardwareStatus();
}
function renderHardwareStatus() {
  const linked = activeDevice().id === hardwareDeviceId && hardware.connected;
  const recording = audio.enabled && (audio.status === 'recording' || audio.status === 'tail');
  const audioLabel = recording ? ' · 电脑录音中' : audio.enabled && audio.status === 'starting' ? ' · 麦克风启动中' : audio.enabled && audio.status === 'error' ? ' · 录音失败' : '';
  const visionLabels = { no_camera: '视觉等待画面', tracking: '视觉识别在线', initializing: '视觉确认状态中', opening_pending: '视觉确认开门中', closing_pending: '视觉确认关门中', multiple_markers: '多个抽屉识别码冲突', error: '视觉识别异常' };
  const label = vision.enabled && activeDevice().id === hardwareDeviceId ? visionLabels[vision.status] || '视觉等待画面' : linked ? hardware.state?.source === 'simulation' ? '模拟设备已连接' : 'K230 已连接' : '设备未连接';
  $('hardware-status').textContent = label + (linked ? audioLabel : '') + (isPhysical() ? ' · ' + recordingLabel() : ' · 预览');
  $('hardware-status').title = linked && audio.error ? audio.error : $('hardware-status').textContent;
  document.querySelector('.footer-end').textContent = isPhysical() ? (inventory.connected ? '已同步' : '未同步') : '预览数据';
  document.querySelector('.bottom-status span').textContent = isPhysical() ? '本机库存' : '本地预览';
  document.querySelector('.preview-label').innerHTML = '<span class="status-dot"></span>' + (isPhysical() ? '自动记录' : '界面预览');
  syncTransferControls();
}
function applyDoorSnapshot(snapshot) {
  if (snapshot?.schema !== 1 || snapshot.cabinet_id !== 'cabinet-01' || typeof snapshot.connected !== 'boolean') return;
  const message = snapshot.state;
  if (message && (!Array.isArray(message.open) || message.open.length !== 3 || message.open.some((value) => typeof value !== 'boolean'))) return;
  const connectionChanged = hardware.connected !== snapshot.connected;
  const knowledgeChanged = JSON.stringify(hardware.state?.known) !== JSON.stringify(message?.known);
  hardware = snapshot;
  const device = state.devices.find((entry) => entry.id === hardwareDeviceId);
  if (device && message && snapshot.connected) {
    const changed = device.open.some((value, index) => value !== message.open[index]);
    device.open = [...message.open];
    if (device.id === activeId && (changed || knowledgeChanged || connectionChanged)) {
      if (Number.isInteger(message.drawer) && message.drawer >= 1 && message.drawer <= 3) selectedDrawer = message.drawer;
      else if (message.source === 'vision' && message.known?.every(Boolean) && message.open.filter(Boolean).length === 1) selectedDrawer = message.open.findIndex(Boolean) + 1;
      renderDock();
      syncModel();
    }
    if (device.id === activeId && activeTab === 'overview' && (changed || knowledgeChanged || connectionChanged)) renderPanel();
  } else if (connectionChanged && activeDevice().id === hardwareDeviceId) {
    renderDock();
    if (activeTab === 'overview') renderPanel();
  }
  renderHardwareStatus();
}
function applyCameraSnapshot(snapshot) {
  if (!snapshot || typeof snapshot.connected !== 'boolean') return;
  const changed = camera.connected !== snapshot.connected || camera.epoch !== snapshot.epoch;
  camera = snapshot;
  if (changed && activeTab === 'overview' && activeDevice().open.some(Boolean)) renderPanel();
}
function setTab(tab) {
  activeTab = tab;
  renderPanel();
  $('panel-content').scrollTop = 0;
}
function switchDevice(id) {
  cabinet?.cancelTransfer();
  clearTimeout(receiptTimer);
  transferFeedback = null;
  $('transfer-label').hidden = true;
  activeId = id;
  selectedDrawer = 1;
  drawerFilter = 0;
  query = '';
  inventoryMatches = null;
  $('command-response').hidden = true;
  render(true);
}
function toggleDrawer() {
  if (transferBusy()) return;
  const device = activeDevice();
  if (isPhysical()) return;
  device.open[selectedDrawer - 1] = !device.open[selectedDrawer - 1];
  if (activeTab !== 'overview') activeTab = 'overview';
  render(); persist();
}
function demoInsert() {
  if (transferBusy() || isPhysical()) return;
  const presets = [['燕麦能量棒', 'cookie'], ['维生素', 'pill'], ['USB-C 充电线', 'cable']];
  const [name, kind] = presets[selectedDrawer - 1];
  const photoSample = selectedDrawer === 1 ? photoFixtures()[demoPhotoIndex++ % 4] : null;
  const item = photoSample
    ? { ...photoSample, id: uid(), storedOn: localDate() }
    : makeItem(uid(), name, selectedDrawer, kind, 0, kind === 'cable' ? '' : localDate(30), '', kind === 'cookie' ? '这个保质期是下个月今天。' : '');
  recordTransfer('in', item);
}
function demoRemove() {
  if (transferBusy() || isPhysical()) return;
  const item = activeDevice().items.find((entry) => entry.drawer === selectedDrawer);
  if (!item) { toast('这个抽屉里暂无可取出的物品'); return; }
  recordTransfer('out', item);
}
function recordTransfer(direction, item) {
  if (transferBusy() || isPhysical()) return;
  const device = activeDevice();
  if (direction === 'out' && !device.items.some((entry) => entry.id === item.id)) return;
  const linked = device.id === hardwareDeviceId && (hardware.connected || vision.enabled);
  if (linked && (!drawerKnown(device, item.drawer - 1) || !device.open[item.drawer - 1])) { toast('请先打开对应抽屉，再演示取放'); return; }
  clearTimeout(receiptTimer);
  const id = uid();
  selectedDrawer = item.drawer;
  if (!linked) device.open[item.drawer - 1] = true;
  if (direction === 'in') device.items.unshift(item);
  else device.items = device.items.filter((entry) => entry.id !== item.id);
  device.activity.unshift({ id, title: (direction === 'in' ? '放入' : '取出') + item.name, drawer: item.drawer, kind: item.kind, photo: item.photo, time: timeNow(), direction, demo: true });
  transferFeedback = { id, itemId: item.id, name: item.name, drawer: item.drawer, direction, phase: 'moving' };
  $('transfer-label').hidden = true;
  $('transfer-name').textContent = item.name;
  $('transfer-direction').textContent = ordinals[item.drawer - 1] + ' / ' + (direction === 'in' ? '正在放入' : '正在取出');
  $('transfer-symbol').innerHTML = itemVisual(item, 'transfer');
  $('transfer-progress').style.transform = 'scaleX(0)';
  // Inventory is committed by this preview action; animation only visualizes that change.
  render(); persist();
  if (panelCollapsed) document.querySelector('.activity-dot').hidden = false;
  const finish = () => {
    if (transferFeedback?.id !== id) return;
    transferFeedback.phase = 'done';
    $('transfer-label').hidden = true;
    renderDock();
    receiptTimer = setTimeout(() => { transferFeedback = null; renderDock(); }, 2600);
  };
  if (!cabinet || document.hidden) { finish(); return; }
  cabinet.playTransfer({ direction, item, drawer: item.drawer, onComplete: finish, onFrame: ({ x, y, progress }) => {
    if (transferFeedback?.id !== id) return;
    const label = $('transfer-label');
    label.hidden = state.reducedMotion || motionPreference.matches;
    const width = $('viewport').clientWidth;
    const height = $('viewport').clientHeight;
    const labelWidth = label.offsetWidth || 172;
    const labelHeight = label.offsetHeight || 80;
    const left = x + 28 + labelWidth < width - 12 ? x + 28 : x - labelWidth - 28;
    label.style.left = Math.max(12, Math.min(width - labelWidth - 12, left)) + 'px';
    label.style.top = Math.max(12, Math.min(height - labelHeight - 12, y - labelHeight / 2)) + 'px';
    $('transfer-progress').style.transform = `scaleX(${progress})`;
  } });
}
async function completeReminder(id) {
  const reminder = activeDevice().reminders.find((entry) => entry.id === id);
  if (!reminder) return;
  if (isPhysical()) {
    try {
      await api(`/api/reminders/${id}/cancel`, { method: 'POST' });
      await refreshReminders(); toast('提醒已取消');
    } catch (error) { toast(error.message); }
    return;
  }
  if (reminder.repeat === 'daily' && !reminder.done) {
    const nextDate = new Date(Math.max(new Date(reminder.date + 'T00:00:00').getTime(), new Date(localDate() + 'T00:00:00').getTime()));
    nextDate.setDate(nextDate.getDate() + 1);
    reminder.date = `${nextDate.getFullYear()}-${String(nextDate.getMonth() + 1).padStart(2, '0')}-${String(nextDate.getDate()).padStart(2, '0')}`;
    toast('本次已完成，下次 ' + dateLabel(reminder.date) + ' ' + reminder.time);
  } else reminder.done = !reminder.done;
  renderPanel(); persist();
}
function openModal(title, html) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  if (!$('modal').open) $('modal').showModal();
  paintIcons();
}
function closeModal() { $('modal').close(); }
const cancelButton = '<button class="button" type="button" data-action="close-modal">取消</button>';
function openItem(id) {
  const device = activeDevice();
  const item = device.items.find((entry) => entry.id === id);
  if (!item) return;
  const preview = `<div class="item-preview">${itemVisual(item, 'detail')}<div class="item-preview-copy"><h3>${esc(item.name)}</h3><p>${ordinals[item.drawer - 1]} · ${esc(device.drawerNames[item.drawer - 1])}</p><small>${esc(item.photo?.sourceLabel || '暂无物品照片')}</small></div></div>`;
  const capture = item.photo?.originalUrl ? `<details class="capture-details"><summary>查看记录画面${icon('chevron-down')}</summary><div class="capture-image"><img data-capture-photo src="${esc(item.photo.originalUrl)}" alt="${esc(item.name)}的原始测试画面" loading="lazy"><span class="capture-error" hidden>记录画面暂不可用</span></div><p>${esc(item.photo.sourceLabel)}</p></details>` : '';
  openModal('物品详情', `${preview}<form id="item-form" class="form-grid"><label class="field">名称<input name="name" value="${esc(item.name)}" maxlength="48" required></label><div class="field-row"><label class="field">所在抽屉<select name="drawer">${ordinals.map((name, index) => `<option value="${index + 1}" ${item.drawer === index + 1 ? 'selected' : ''}>${name}</option>`).join('')}</select></label><label class="field">到期日期<input name="expiry" type="date" value="${esc(item.expiry)}"></label></div><label class="field">备注<textarea name="note" maxlength="200">${esc(item.note)}</textarea></label>${item.voice ? `<div class="source-quote"><span>随物品记录的原话</span>“${esc(item.voice)}”</div>` : ''}${capture}<div class="modal-actions"><button class="button danger" type="button" id="remove-item">${icon('arrow-up-right')}标记已取出</button>${cancelButton}<button class="button primary" type="submit">保存</button></div></form>`);
  const editRevision = inventory.revision;
  $('item-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const name = String(form.get('name')).trim();
    if (!name) { event.currentTarget.elements.name.setCustomValidity('请填写物品名称'); event.currentTarget.reportValidity(); return; }
    if (isPhysical()) {
      try {
        await api('/api/inventory/items/' + item.id, { method: 'PATCH', body: {
          expected_version: item.version, base_revision: editRevision, idempotency_key: uid(),
          patch: { name, location_id: 'cabinet-01:drawer-' + Number(form.get('drawer')),
            expires_on: String(form.get('expiry')) || null, notes: String(form.get('note')).trim() },
        } });
        closeModal(); await refreshInventory(); toast('已保存');
      } catch (error) { toast(error.message); }
      return;
    }
    Object.assign(item, { name, drawer: Number(form.get('drawer')), expiry: String(form.get('expiry')), note: String(form.get('note')).trim() });
    persist(); closeModal(); render(); toast('已保存到本地预览');
  });
  $('remove-item').addEventListener('click', async () => {
    if (isPhysical()) {
      try {
        await api('/api/inventory/items/' + item.id, { method: 'PATCH', body: {
          expected_version: item.version, base_revision: editRevision, idempotency_key: uid(),
          patch: { presence_status: 'removed' },
        } });
        closeModal(); await refreshInventory(); toast('已标记取出');
      } catch (error) { toast(error.message); }
      return;
    }
    closeModal(); recordTransfer('out', item);
  });
  syncTransferControls();
}
async function api(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(6000), ...options,
    headers: { 'Content-Type': 'application/json' }, body: options.body ? JSON.stringify(options.body) : undefined });
  if (!response.ok) {
    if (response.status === 409) throw new Error('记录已变化或仍需核对，请刷新后重试');
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === 'string' ? payload.detail : '操作未完成，请检查连接和填写内容');
  }
  return response.json();
}
function openManualAdd() {
  if (!isPhysical() || !inventory.connected) { toast('库存服务未连接'); return; }
  const revision = inventory.revision;
  openModal('补录物品', `<form id="manual-item-form" class="form-grid"><label class="field">名称<input name="name" maxlength="120" required></label><label class="field">所在抽屉<select name="drawer">${ordinals.map((name, i) => `<option value="${i + 1}" ${selectedDrawer === i + 1 ? 'selected' : ''}>${name}</option>`).join('')}</select></label><label class="field">备注<textarea name="note" maxlength="1000"></textarea></label><div class="modal-actions">${cancelButton}<button class="button primary" type="submit">保存</button></div></form>`);
  $('manual-item-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api('/api/inventory/items', { method: 'POST', body: { base_revision: revision,
        idempotency_key: uid(), values: { name: String(form.get('name')).trim(),
          name_kind: 'attribute_description', location_id: 'cabinet-01:drawer-' + form.get('drawer'),
          notes: String(form.get('note')).trim() } } });
      closeModal(); await refreshInventory(); toast('已补录');
    } catch (error) { toast(error.message); }
  });
}
const sessionLabels = { capturing: '记录中', queued: '等待识别', processing: '识别中', committed: '已同步', pending: '待核对', failed: '识别失败', dismissed: '已忽略' };
const reasonLabels = {
  capture_process_interrupted: '记录服务中途停止', bridge_or_camera_disconnected: '设备连接中断',
  camera_epoch_changed: '摄像头重新连接', camera_stalled: '画面停止更新',
  drawer_identity_uncertain: '抽屉编号不确定', frame_gap: '操作画面有缺失',
  opening_without_observed_closed_state: '启动时抽屉已打开', insufficient_frames: '有效画面不足',
  entry_boundary_or_released_placement_missing: '放入路径或松手画面不完整',
  drawer_to_video_edge_to_exit_not_observed: '没有拍到完整取出路径',
  physical_identity_uncertain: '物品身份不确定', existing_item_match_required: '需要核对已有物品',
  return_identity_match_required: '需要核对放回的物品', physical_identity_or_action_uncertain: '物品或动作不确定',
  inventory_changed_during_analysis: '识别期间库存被修改', conflicting_observations: '前后画面判断有冲突',
};
function reasonText(reason) {
  const value = typeof reason === 'string' ? reason : reason.reason;
  return reasonLabels[value] || '需要人工核对';
}
function openRecording() {
  openModal('自动记录', `<div class="session-list">${recording.sessions.length ? recording.sessions.map((session) => `<button class="session-row" data-session="${esc(session.id)}"><span class="row-copy"><strong>${esc(sessionLabels[session.state])}</strong><small>${esc(new Date(session.created_at).toLocaleString('zh-CN'))}</small></span>${icon('chevron-right')}</button>`).join('') : '<p class="field-note">暂无操作记录</p>'}</div>`);
}
async function openSession(identity) {
  try {
    const session = await api('/api/recording/sessions/' + identity);
    const frames = session.frames;
    const reviewable = ['pending', 'failed'].includes(session.state);
    openModal('收纳记录 · ' + sessionLabels[session.state], `<div class="session-detail">${session.detail.reasons?.length ? `<p class="session-reasons">${session.detail.reasons.map((reason) => esc(reasonText(reason))).join('；')}</p>` : ''}${session.detail.message ? `<p>${esc(session.detail.message)}</p>` : ''}${session.objects?.length ? `<p>${session.objects.map((item) => esc(item.name)).join('、')}</p>` : ''}${frames.length ? `<div class="capture-image session-image"><img id="session-frame" src="${esc(frames[0].url)}" alt="收纳记录画面"></div><label class="session-timeline"><input id="session-seek" type="range" min="0" max="${frames.length - 1}" step="1" value="0" aria-label="查看操作画面"><output id="session-time">${(frames[0].media_time_ms / 1000).toFixed(2)} 秒</output></label>` : '<p>没有可用画面</p>'}<div class="modal-actions"><button class="button" data-action="recording">返回</button>${reviewable ? `<button class="button" data-session-action="dismiss" data-session-id="${esc(identity)}">核对后忽略</button><button class="icon-button" data-session-action="retry" data-session-id="${esc(identity)}" aria-label="重试识别" title="重试识别">${icon('refresh-cw')}</button>` : ''}</div></div>`);
    if (session.voice?.length) {
      const section = document.createElement('div');
      const labels = { waiting_inventory: '等待库存识别', processing: '正在处理', completed: '已处理', failed: '处理失败' };
      section.innerHTML = session.voice.map((entry) => `<p><strong>收纳语音 · ${labels[entry.state]}</strong></p>
        ${entry.transcript ? `<p>${esc(entry.transcript)}</p>` : ''}<p>${esc(entry.result.answer || entry.result.error || '')}</p>
        ${entry.state === 'failed' ? `<button class="icon-button" data-retry-voice="${esc(entry.id)}" aria-label="重试语音处理" title="重试语音处理">${icon('refresh-cw')}</button>` : ''}`).join('');
      document.querySelector('.session-detail').prepend(section);
      paintIcons();
    }
    $('session-seek')?.addEventListener('input', (event) => {
      const frame = frames[Number(event.target.value)];
      $('session-frame').src = frame.url;
      $('session-time').textContent = (frame.media_time_ms / 1000).toFixed(2) + ' 秒';
    });
  } catch (error) { toast(error.message); }
}
async function changeSession(identity, action) {
  try {
    await api(`/api/recording/sessions/${identity}/${action}`, { method: 'POST' });
    await refreshRecording(); openRecording(); toast(action === 'retry' ? '已加入识别队列' : '已忽略本次后续核对');
  } catch (error) { toast(error.message); }
}
function uiItem(item) {
  const match = /:drawer-([123])$/.exec(item.location_id || '');
  return { id: item.id, name: item.name, drawer: match ? Number(match[1]) : 0, kind: 'package',
    version: item.version, storedOn: item.stored_at ? new Date(item.stored_at).toLocaleDateString('sv-SE') : '',
    expiry: item.expires_on || '', note: item.notes || item.description || '', photo: item.photo };
}
async function refreshInventory() {
  const value = await api('/api/inventory');
  const signature = JSON.stringify(value);
  const changed = signature !== inventorySignature || !inventory.connected;
  inventory = { connected: true, revision: value.inventory_revision, known: value.inventory_known };
  if (changed) {
    inventorySignature = signature;
    physicalDevice.items = value.items.map(uiItem);
    const verbs = { add: '记录', remove: '取出', return: '放回', update: '更新', keep: '确认' };
    physicalDevice.activity = value.activity.map((event) => ({ id: event.id,
      title: `${verbs[event.action] || '更新'} ${event.item.name}`, drawer: uiItem(event.item).drawer,
      direction: event.action === 'remove' ? 'out' : 'in',
      time: new Date(event.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }));
    if (isPhysical()) render(); else renderDevices();
  }
}
async function refreshRecording() {
  const value = await api('/api/recording/state');
  const signature = JSON.stringify(value);
  recording = value;
  if (signature !== recordingSignature) {
    recordingSignature = signature;
    if (isPhysical() && activeTab === 'overview') renderPanel();
    const strip = document.querySelector('.recording-strip span');
    if (strip) strip.innerHTML = `<span class="status-dot ${inventory.connected ? 'active' : ''}"></span>${esc(recordingLabel())}`;
  }
}
async function searchInventory() {
  const term = query;
  if (!isPhysical() || !term.trim()) { inventoryMatches = null; return; }
  const value = await api('/api/inventory?query=' + encodeURIComponent(term));
  if (!isPhysical() || query !== term) return;
  inventoryMatches = value.items.map((item) => item.id);
  if ($('item-results')) { $('item-results').innerHTML = itemRows(); paintIcons(); }
}
async function acknowledgeDelivery(entry, kind) {
  return api(`/api/reminders/deliveries/${entry.id}/ack`, { method: 'POST', body: { channel: entry.channel, kind } });
}
function playReminder(entry) {
  if (!('speechSynthesis' in window)) return;
  attemptedDeliveries.add(entry.id);
  const utterance = new SpeechSynthesisUtterance(entry.message);
  utterance.lang = 'zh-CN';
  utterance.onstart = () => acknowledgeDelivery(entry, 'received').catch(() => {});
  utterance.onend = () => acknowledgeDelivery(entry, 'played').then(refreshReminders).catch(() => {});
  utterance.onerror = () => acknowledgeDelivery(entry, 'failed').then(refreshReminders).catch(() => {});
  queueSpeech(utterance);
}
async function refreshReminders() {
  const value = await api('/api/reminders');
  const signature = JSON.stringify(value);
  pendingDeliveries = value.deliveries;
  physicalDevice.reminders = value.reminders.map((entry) => {
    const date = new Date(entry.due_at);
    return { id: entry.id, title: entry.message, date: date.toLocaleDateString('sv-SE'),
      time: date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }),
      repeat: entry.repeat, done: entry.status !== 'pending', status: entry.status };
  });
  for (const entry of pendingDeliveries) {
    if (entry.channel === 'web' && !attemptedDeliveries.has(entry.id)) {
      attemptedDeliveries.add(entry.id); toast(entry.message);
      answer('提醒：' + entry.message); $('command-panel').hidden = false;
      acknowledgeDelivery(entry, 'received').catch(() => {});
    }
    if (entry.channel === 'voice' && entry.state !== 'failed' && !attemptedDeliveries.has(entry.id)
        && !voiceQuery.recording && !['recording', 'processing'].includes(wake.status)
        && !['recording', 'starting', 'tail'].includes(audio.status)) playReminder(entry);
  }
  if (signature !== reminderSignature) {
    reminderSignature = signature;
    if (isPhysical() && activeTab !== 'items') renderPanel();
  }
}
async function refreshVoice() {
  const value = await api('/api/voice/state');
  const interactions = value.interactions.filter((entry) => ['door', 'wake'].includes(entry.origin));
  if (!voiceInitialized) {
    interactions.filter((entry) => ['completed', 'failed'].includes(entry.state)).forEach((entry) => seenDoorResults.add(entry.id));
    voiceInitialized = true;
  }
  for (const entry of [...interactions].reverse()) {
    if (!['completed', 'failed'].includes(entry.state) || seenDoorResults.has(entry.id)) continue;
    if (entry.origin === 'wake' && document.visibilityState !== 'visible') continue;
    seenDoorResults.add(entry.id);
    const message = entry.result.answer || entry.result.error;
    if (message) {
      $('command-panel').hidden = false;
      answer(`${entry.transcript ? (entry.origin === 'wake' ? '你说：' : '收纳时说：') + entry.transcript + '\n\n' : ''}${message}`);
      if (entry.state === 'completed' && !voiceQuery.recording && !['recording', 'starting', 'tail'].includes(audio.status)) {
        await speakInteraction(entry, message);
      }
      setVoiceStatus(entry.state === 'completed' ? '语音已处理' : '语音处理失败');
    }
  }
  if (interactions[0]?.state === 'waiting_inventory' && !voiceQuery.busy && !voiceQuery.recording) setVoiceStatus('收纳语音等待库存识别');
  if (['queued', 'processing'].includes(interactions[0]?.state) && !voiceQuery.busy && !voiceQuery.recording) setVoiceStatus('正在处理语音');
}
async function pollInventory() {
  try { await refreshInventory(); await refreshRecording(); await refreshReminders(); await refreshVoice(); }
  catch {
    if (inventory.connected) {
      inventory = { ...inventory, connected: false };
      if (isPhysical()) renderPanel();
    }
  } finally { renderHardwareStatus(); setTimeout(pollInventory, 1200); }
}
function openReminder(id = '', preset = {}) {
  const device = activeDevice();
  const existing = device.reminders.find((entry) => entry.id === id);
  const reminder = existing || { title: '', date: localDate(), time: '', repeat: 'once', ...preset };
  openModal(existing ? '编辑提醒' : '添加提醒', `<form id="reminder-form" class="form-grid"><label class="field">提醒内容<input name="title" value="${esc(reminder.title)}" maxlength="80" required placeholder="吃鱼油"></label><div class="field-row"><label class="field">日期<input name="date" type="date" value="${esc(reminder.date)}" required></label><label class="field">时间<input name="time" type="time" value="${esc(reminder.time)}" required></label></div><label class="field">重复<select name="repeat"><option value="once" ${reminder.repeat === 'once' ? 'selected' : ''}>仅一次</option><option value="daily" ${reminder.repeat === 'daily' ? 'selected' : ''}>每天</option></select></label><div class="modal-actions">${existing ? `<button class="button danger" type="button" id="delete-reminder">${icon('trash-2')}删除</button>` : ''}${cancelButton}<button class="button primary" type="submit">保存</button></div></form>`);
  $('reminder-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const title = String(form.get('title')).trim();
    if (!title) { event.currentTarget.elements.title.setCustomValidity('请填写提醒内容'); event.currentTarget.reportValidity(); return; }
    const values = { title, date: String(form.get('date')), time: String(form.get('time')), repeat: String(form.get('repeat')) };
    if (device.id === hardwareDeviceId) {
      try {
        await api('/api/reminders', { method: 'POST', body: { message: title,
          due_at: new Date(values.date + 'T' + values.time).toISOString(), repeat: values.repeat, idempotency_key: uid() } });
        if (existing) await api(`/api/reminders/${existing.id}/cancel`, { method: 'POST' });
        closeModal(); activeTab = 'reminders'; setPanel(false); await refreshReminders(); render(); toast('提醒已保存');
      } catch (error) { toast(error.message); }
      return;
    }
    if (existing) Object.assign(existing, values);
    else device.reminders.push({ ...values, id: uid(), done: false });
    persist(); closeModal(); activeTab = 'reminders'; setPanel(false); render(); toast('提醒已保存到本地预览');
  });
  $('delete-reminder')?.addEventListener('click', async () => {
    if (device.id === hardwareDeviceId) {
      try {
        await api(`/api/reminders/${id}/cancel`, { method: 'POST' });
        closeModal(); await refreshReminders(); toast('提醒已取消');
      } catch (error) { toast(error.message); }
      return;
    }
    device.reminders = device.reminders.filter((entry) => entry.id !== id);
    persist(); closeModal(); renderPanel(); toast('已删除预览提醒');
  });
}
function openAddDevice() {
  openModal('添加设备', `<form id="device-form" class="form-grid"><label class="field">设备名称<input name="name" maxlength="30" required placeholder="阳台收纳柜" autocomplete="off"></label><label class="field">柜体类型<select name="type"><option>三层抽屉柜</option></select></label><p class="field-note">使用简化模型添加到本地预览，尚未绑定真实硬件。</p><div class="modal-actions">${cancelButton}<button class="button primary" type="submit">添加到预览</button></div></form>`);
  $('device-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get('name')).trim();
    if (!name) { event.currentTarget.elements.name.setCustomValidity('请填写设备名称'); event.currentTarget.reportValidity(); return; }
    const device = { id: uid(), code: String(state.devices.length + 1).padStart(2, '0'), name, open: [false, false, false], drawerNames: [...drawerNames], items: [], reminders: [], activity: [] };
    state.devices.push(device); persist(); closeModal(); switchDevice(device.id); setTab('overview');
    toast('已添加到本地预览');
  });
}
function settings() {
  openModal('设置', `<div class="setting-row"><span>外观</span><div class="theme-switch" aria-label="主题">${[['light', 'sun', '浅色'], ['dark', 'moon', '深色'], ['system', 'monitor', '跟随系统']].map(([value, symbol, label]) => `<button data-theme-choice="${value}" class="${state.theme === value ? 'selected' : ''}" aria-label="${label}" aria-pressed="${state.theme === value}" title="${label}">${icon(symbol)}</button>`).join('')}</div></div><div class="setting-row"><span>物品建议</span><label class="toggle"><input type="checkbox" id="suggestions-toggle" aria-label="物品建议" ${state.suggestions ? 'checked' : ''}><span></span></label></div><div class="setting-row"><span>减少动态效果</span><label class="toggle"><input type="checkbox" id="motion-toggle" aria-label="减少动态效果" ${state.reducedMotion ? 'checked' : ''}><span></span></label></div><div class="demo-settings"><h3>预览控制</h3><div class="demo-actions"><button class="button" data-action="demo-drawer">${icon('play')}抽屉开合</button><button class="button" data-action="demo-insert">${icon('package-plus')}放入物品</button><button class="button" data-action="demo-remove">${icon('package-minus')}取出物品</button></div></div>`);
  $('suggestions-toggle').addEventListener('change', (event) => { state.suggestions = event.target.checked; persist(); renderPanel(); });
  const visionRow = document.createElement('div');
  visionRow.className = 'setting-row';
  visionRow.innerHTML = '<span>抽屉视觉识别</span><button class="button" id="vision-setup">' + icon('monitor') + '视觉识别</button>';
  $('modal-body').prepend(visionRow);
  $('vision-setup').addEventListener('click', () => { location.href = '/vision.html'; });
  const wakeRow = document.createElement('div');
  wakeRow.className = 'setting-row';
  wakeRow.innerHTML = `<span>中文唤醒 · ${esc(wake.word || '你好小盒')}</span><label class="toggle"><input type="checkbox" id="wake-toggle" aria-label="中文语音唤醒" ${wake.enabled ? 'checked' : ''}><span></span></label>`;
  $('modal-body').prepend(wakeRow);
  $('wake-toggle').addEventListener('change', async (event) => {
    try { wake = await api('/api/audio/wake/control', { method: 'POST', body: { enabled: event.target.checked } }); }
    catch (error) { event.target.checked = wake.enabled; toast(error.message); }
  });
  paintIcons();
  $('motion-toggle').addEventListener('change', (event) => { state.reducedMotion = event.target.checked; persist(); syncModel(); syncTransferControls(); });
  syncTransferControls();
}
function commandOpen() {
  $('command-panel').hidden = false;
  $('command-input').focus();
}
function answer(text) {
  $('command-response').textContent = text;
  $('command-response').hidden = !text;
  $('play-voice-answer').hidden = true;
  lastSpokenAnswer = '';
}
function setVoiceStatus(text) { $('voice-status').textContent = text; }
function setVoiceButton() {
  const button = $('record-command');
  if (!button) return;
  button.classList.toggle('recording', voiceQuery.recording);
  button.classList.toggle('busy', voiceQuery.busy);
  button.disabled = voiceQuery.busy;
  button.setAttribute('aria-label', voiceQuery.recording ? '停止录音' : '开始录音');
  button.dataset.tooltip = voiceQuery.recording ? '停止录音' : '开始录音';
  button.innerHTML = icon(voiceQuery.recording ? 'square' : 'mic');
  paintIcons();
}
async function updateWakeLease() {
  const response = await fetch('/api/audio/wake/control', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, signal: AbortSignal.timeout(3000),
    body: JSON.stringify({ client: microphoneClient,
      paused: speechJobs.size > 0 || voiceQuery.recording || voiceQuery.busy }) });
  if (!response.ok && response.status !== 404) throw new Error('无法暂停唤醒检测');
}
async function queueSpeech(utterance) {
  const job = {};
  speechJobs.add(job);
  let timer;
  const finish = () => {
    clearTimeout(timer);
    speechJobs.delete(job);
    updateWakeLease().catch(() => {});
  };
  const ended = utterance.onend;
  const failed = utterance.onerror;
  utterance.onend = (event) => { finish(); ended?.(event); };
  utterance.onerror = (event) => { finish(); failed?.(event); setVoiceStatus('可点击播放回答'); };
  // Acquire the pause before audio starts; renew it while speech is queued or playing.
  try {
    await updateWakeLease();
    if (!speechJobs.has(job)) return;
    timer = setTimeout(finish, 180000);
    window.speechSynthesis.speak(utterance);
  } catch {
    finish();
    setVoiceStatus('播报暂不可用，可点击播放回答');
  }
}
async function speakInteraction(entry, message) {
  const claim = async () => {
    const key = 'mindbox-spoken-' + entry.id;
    try {
      if (localStorage.getItem(key)) return;
      localStorage.setItem(key, String(Date.now()));
    } catch { /* Storage can be unavailable in private browser sessions. */ }
    speakAnswer(message);
  };
  if (navigator.locks) await navigator.locks.request('mindbox-speech', claim);
  else await claim();
}
function speakAnswer(text) {
  if (!('speechSynthesis' in window) || typeof SpeechSynthesisUtterance === 'undefined') return false;
  lastSpokenAnswer = text;
  $('play-voice-answer').hidden = false;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'zh-CN';
  utterance.rate = .98;
  queueSpeech(utterance);
  return true;
}
async function sendVoiceAudio(blob) {
  voiceQuery.busy = true;
  setVoiceStatus('正在理解并处理');
  setVoiceButton();
  try {
    const response = await fetch('/api/voice/query', {
      method: 'POST',
      headers: { 'Content-Type': blob.type || 'audio/webm', 'X-Mind-Box-Cabinet': activeDevice().id === hardwareDeviceId ? 'cabinet-01' : activeDevice().id },
      body: blob,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `语音服务返回 ${response.status}`);
    answer(`你说：${payload.transcript}\n\n${payload.answer}`);
    await refreshInventory(); await refreshReminders();
    setVoiceStatus(speakAnswer(payload.answer) ? '回答已播报' : '回答已返回，浏览器不支持语音合成');
  } catch (error) {
    answer('语音提问失败：' + error.message);
    setVoiceStatus('语音服务不可用');
  } finally {
    voiceQuery.busy = false;
    setVoiceButton();
    updateWakeLease().catch(() => {});
  }
}
function stopVoiceRecording() {
  if (!voiceQuery.recorder || voiceQuery.recorder.state === 'inactive') return;
  voiceQuery.recorder.stop();
  voiceQuery.recording = false;
  setVoiceStatus('正在整理录音');
  setVoiceButton();
}
async function toggleVoiceRecording() {
  if (voiceQuery.busy) return;
  if (voiceQuery.recording) { stopVoiceRecording(); return; }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
    answer('当前浏览器不支持麦克风录音。');
    return;
  }
  try {
    window.speechSynthesis?.cancel();
    speechJobs.clear();
    voiceQuery.busy = true;
    await updateWakeLease();
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    const options = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? { mimeType: 'audio/webm;codecs=opus' } : {};
    const recorder = new MediaRecorder(stream, options);
    voiceQuery = { recorder, stream, chunks: [], recording: true, busy: false };
    recorder.addEventListener('dataavailable', (event) => { if (event.data.size) voiceQuery.chunks.push(event.data); });
    recorder.addEventListener('stop', () => {
      stream.getTracks().forEach((track) => track.stop());
      const blob = new Blob(voiceQuery.chunks, { type: recorder.mimeType || 'audio/webm' });
      voiceQuery.stream = null;
      voiceQuery.recorder = null;
      if (blob.size) sendVoiceAudio(blob);
      else { setVoiceStatus('没有录到声音'); updateWakeLease().catch(() => {}); }
    }, { once: true });
    recorder.start(250);
    setVoiceStatus('正在录音，再次点击结束');
    setVoiceButton();
  } catch (error) {
    voiceQuery.busy = false;
    updateWakeLease().catch(() => {});
    setVoiceStatus('麦克风权限未开启');
    answer('无法访问电脑麦克风：' + error.message);
  }
}
async function submitCommand(event) {
  event.preventDefault();
  const raw = $('command-input').value.trim();
  if (!raw || voiceQuery.busy || voiceQuery.recording) return;
  voiceQuery.busy = true; setVoiceButton(); setVoiceStatus('正在处理');
  try {
    const payload = await api('/api/voice/text', { method: 'POST', signal: AbortSignal.timeout(600000),
      body: { text: raw, cabinet_id: 'cabinet-01' } });
    answer(payload.answer);
    await refreshInventory(); await refreshReminders();
    if (!isPhysical()) switchDevice(hardwareDeviceId);
    const first = payload.inventory?.items?.[0];
    const item = first && physicalDevice.items.find((entry) => entry.id === first.id);
    if (item) {
      selectedDrawer = item.drawer || 1; drawerFilter = item.drawer;
      query = item.name; inventoryMatches = payload.inventory.items.map((entry) => entry.id);
      activeTab = 'items'; setPanel(false); render();
    }
    setVoiceStatus('处理完成');
  } catch (error) { answer('处理失败：' + error.message); setVoiceStatus('服务不可用'); }
  finally { voiceQuery.busy = false; setVoiceButton(); }
}

document.addEventListener('load', (event) => {
  if (event.target.matches?.('[data-item-photo]')) event.target.closest('.item-visual').classList.add('photo-ready');
}, true);
document.addEventListener('error', (event) => {
  const image = event.target;
  if (image.matches?.('[data-item-photo]')) {
    image.closest('.item-visual').classList.remove('photo-ready');
    image.hidden = true;
  }
  if (image.matches?.('[data-capture-photo]')) {
    image.hidden = true;
    image.nextElementSibling.hidden = false;
  }
}, true);

document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.device) switchDevice(button.dataset.device);
  if (button.dataset.selectDrawer) selectDrawer(Number(button.dataset.selectDrawer));
  if (button.dataset.viewDrawer) { selectDrawer(Number(button.dataset.viewDrawer)); drawerFilter = selectedDrawer; query = ''; setTab('items'); }
  if (button.dataset.tab) setTab(button.dataset.tab);
  if (button.dataset.filter != null) { drawerFilter = Number(button.dataset.filter); if (drawerFilter) selectDrawer(drawerFilter); renderPanel(); }
  if (button.dataset.item) openItem(button.dataset.item);
  if (button.dataset.session) openSession(button.dataset.session);
  if (button.dataset.sessionAction) changeSession(button.dataset.sessionId, button.dataset.sessionAction);
  if (button.dataset.reminder) openReminder(button.dataset.reminder);
  if (button.dataset.complete) completeReminder(button.dataset.complete);
  if (button.dataset.playReminder) {
    const entry = pendingDeliveries.find((item) => item.id === button.dataset.playReminder);
    if (entry) playReminder(entry);
  }
  if (button.dataset.retryVoice) {
    seenDoorResults.delete(button.dataset.retryVoice);
    api(`/api/voice/interactions/${button.dataset.retryVoice}/retry`, { method: 'POST' })
      .then(() => { closeModal(); toast('收纳语音已加入处理队列'); }).catch((error) => toast(error.message));
  }
  if (button.dataset.themeChoice) { state.theme = button.dataset.themeChoice; persist(); applyTheme(); settings(); }
  const actions = {
    'next-suggestion': () => { suggestionIndex++; renderPanel(); },
    'view-reminders': () => setTab('reminders'),
    'add-reminder': () => openReminder(),
    'recording': openRecording,
    'manual-add': openManualAdd,
    'close-modal': closeModal,
    'demo-drawer': () => { closeModal(); toggleDrawer(); },
    'demo-insert': () => { closeModal(); demoInsert(); },
    'demo-remove': () => { closeModal(); demoRemove(); },
  };
  actions[button.dataset.action]?.();
});
document.addEventListener('input', (event) => {
  if (event.target.id === 'item-search') {
    query = event.target.value; inventoryMatches = null;
    $('item-results').innerHTML = itemRows(); paintIcons();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchInventory().catch((error) => toast(error.message)), 180);
  }
  if (event.target.matches('input[name="name"], input[name="title"]')) event.target.setCustomValidity('');
});
document.querySelector('.panel-tabs').addEventListener('keydown', (event) => {
  const tabs = ['overview', 'items', 'reminders'];
  if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const index = tabs.indexOf(activeTab);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
  setTab(tabs[next]); $('tab-' + tabs[next]).focus();
});
$('collapse-panel').addEventListener('click', () => setPanel(true, true));
$('expand-panel').addEventListener('click', () => setPanel(false, true));
$('theme-button').addEventListener('click', () => { state.theme = resolvedTheme() === 'dark' ? 'light' : 'dark'; persist(); applyTheme(); });
$('settings-button').addEventListener('click', settings);
$('add-device').addEventListener('click', openAddDevice);
$('demo-drawer').addEventListener('click', toggleDrawer);
$('demo-insert').addEventListener('click', demoInsert);
$('demo-remove').addEventListener('click', demoRemove);
$('reset-view').addEventListener('click', () => cabinet?.resetView());
$('command-button').addEventListener('click', commandOpen);
$('close-command').addEventListener('click', () => { $('command-panel').hidden = true; $('command-button').focus(); });
$('record-command').addEventListener('click', toggleVoiceRecording);
$('command-form').addEventListener('submit', submitCommand);
$('close-modal').addEventListener('click', closeModal);
$('modal').addEventListener('click', (event) => { if (event.target === $('modal')) { const box = $('modal').getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeModal(); } });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !$('modal').open) $('command-panel').hidden = true; });
systemDark.addEventListener('change', () => { if (state.theme === 'system') applyTheme(); });
motionPreference.addEventListener('change', () => { syncModel(); syncTransferControls(); });
mobile.addEventListener('change', (event) => setPanel(event.matches));
function updateClock() { $('clock').textContent = new Date().toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) + '  ' + timeNow(); }
updateClock(); setInterval(updateClock, 30000);
applyTheme(); setPanel(panelCollapsed); render();
if (needsPhotoSamples) persist();
let doorEvents;
function disconnectDoorEvents() {
  const wasConnected = hardware.connected;
  hardware = { ...hardware, connected: false };
  renderHardwareStatus();
  if (wasConnected && activeDevice().id === hardwareDeviceId) renderDock();
  if (wasConnected && activeDevice().id === hardwareDeviceId && activeTab === 'overview') renderPanel();
}
function connectDoorEvents() {
  doorEvents?.close();
  if (!navigator.onLine) { disconnectDoorEvents(); return; }
  doorEvents = new EventSource('/api/doors/events');
  doorEvents.addEventListener('doors', (event) => {
    try { applyDoorSnapshot(JSON.parse(event.data)); }
    catch (error) { console.warn('Invalid door snapshot:', error); }
  });
  doorEvents.onerror = disconnectDoorEvents;
}
window.addEventListener('offline', () => { doorEvents?.close(); disconnectDoorEvents(); });
window.addEventListener('online', connectDoorEvents);
connectDoorEvents();
async function pollCamera() {
  try {
    const response = await fetch('/api/camera/state', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    if (!response.ok) throw new Error('camera state ' + response.status);
    applyCameraSnapshot(await response.json());
    const image = document.querySelector('.camera-live');
    if (camera.connected && image?.complete && !image.naturalWidth) image.src = '/api/camera/mjpeg?retry=' + Date.now();
  } catch (error) {
    applyCameraSnapshot({ connected: false, latest: null, cached_frames: 0 });
  } finally {
    setTimeout(pollCamera, 1000);
  }
}
pollCamera();
async function pollAudio() {
  try {
    const response = await fetch('/api/audio/state', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    if (!response.ok) throw new Error('audio state ' + response.status);
    const snapshot = await response.json();
    if (typeof snapshot.enabled !== 'boolean' || typeof snapshot.status !== 'string') throw new Error('Invalid audio state');
    audio = snapshot;
  } catch (error) {
    audio = { enabled: false, status: 'unavailable' };
  } finally {
    renderHardwareStatus();
    setTimeout(pollAudio, 1000);
  }
}
pollAudio();
const wakeLabels = { loading: '正在加载中文唤醒', listening: '等待唤醒', recording: '正在听，请讲',
  processing: '正在处理语音', paused: '唤醒已暂停', disabled: '中文唤醒已关闭',
  no_speech: '没有听到问题', error: '中文唤醒不可用', unavailable: '语音服务未连接' };
async function pollWake() {
  try {
    const next = await api('/api/audio/wake/state');
    if (next.event > wake.event && wake.word) {
      $('command-panel').hidden = false;
      answer('');
    }
    const changed = next.status !== wake.status;
    wake = next;
    $('wake-indicator').textContent = wake.enabled ? (wake.status === 'listening' ? wake.word : wakeLabels[wake.status] || '中文唤醒') : '问问 mind box';
    $('wake-indicator').title = wake.error || wakeLabels[wake.status] || '';
    if (changed && !voiceQuery.recording && !voiceQuery.busy && speechJobs.size === 0) setVoiceStatus(wakeLabels[wake.status] || '');
  } catch { wake = { ...wake, status: 'unavailable' }; }
  finally { setTimeout(pollWake, 350); }
}
pollWake();
setInterval(() => {
  if (speechJobs.size || voiceQuery.recording || voiceQuery.busy) updateWakeLease().catch(() => {});
}, 3000);
$('play-voice-answer').addEventListener('click', () => { if (lastSpokenAnswer) speakAnswer(lastSpokenAnswer); });
window.addEventListener('pagehide', () => {
  navigator.sendBeacon('/api/audio/wake/control', JSON.stringify({ client: microphoneClient, paused: false }));
});
async function pollVision() {
  try {
    const response = await fetch('/api/vision/state', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    if (!response.ok) throw new Error('vision state ' + response.status);
    const snapshot = await response.json();
    const changed = vision.enabled !== snapshot.enabled;
    vision = snapshot;
    if (changed) { renderDock(); if (activeTab === 'overview') renderPanel(); }
  } catch {
    if (vision.enabled) vision = { ...vision, status: 'no_camera' };
  } finally {
    renderHardwareStatus();
    setTimeout(pollVision, 1000);
  }
}
pollVision();
pollInventory();
import('./cabinet.js').then(({ createCabinet }) => {
  cabinet = createCabinet($('viewport'), selectDrawer);
  syncModel(true);
}).catch((error) => {
  $('model-fallback').hidden = false;
  console.warn('Cabinet renderer unavailable:', error);
});

// Read-only diagnostics for browser verification, with no production inventory writes.
window.mindBoxPreview = { snapshot: () => ({ device: activeId, selectedDrawer, open: [...activeDevice().open], panelCollapsed, theme: resolvedTheme(), itemCount: activeDevice().items.length, items: activeDevice().items, inventory, recording, transfer: transferFeedback ? { ...transferFeedback } : null, hardware, camera, audio, vision, model: cabinet?.snapshot() }) };
