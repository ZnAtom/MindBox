import './style.css';
import { createIcons, Box, Moon, Sun, Settings2, RotateCcw, Play, Archive, Plus, PanelRightClose, PanelRightOpen, Check, CircleDashed, Mic, HardDrive, MessageCircle, X, ArrowUp, ChevronRight, Sparkles, RefreshCw, ArrowDownLeft, ArrowUpRight, CameraOff, PackagePlus, Search, Cable, Cookie, Pill, Milk, Coffee, Package, Headphones, BatteryCharging, Smartphone, NotebookPen, Monitor, Trash2, Bell, CheckCheck } from 'lucide';

const $ = (id) => document.getElementById(id);
const icons = { Box, Moon, Sun, Settings2, RotateCcw, Play, Archive, Plus, PanelRightClose, PanelRightOpen, Check, CircleDashed, Mic, HardDrive, MessageCircle, X, ArrowUp, ChevronRight, Sparkles, RefreshCw, ArrowDownLeft, ArrowUpRight, CameraOff, PackagePlus, Search, Cable, Cookie, Pill, Milk, Coffee, Package, Headphones, BatteryCharging, Smartphone, NotebookPen, Monitor, Trash2, Bell, CheckCheck };
const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;
const paintIcons = () => createIcons({ icons, attrs: { 'aria-hidden': 'true' } });
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const storageKey = 'mind-box-preview-v2';
const drawerNames = ['零食与补给', '日常用品', '数码与随身'];
const ordinals = ['第一层', '第二层', '第三层'];
const systemDark = matchMedia('(prefers-color-scheme: dark)');
const mobile = matchMedia('(max-width: 760px)');
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
function fixtures() {
  return {
    version: 2, theme: 'light', suggestions: true, reducedMotion: false,
    devices: [
      { id: 'mb-01', code: '01', name: '宿舍收纳柜', open: [false, false, false], drawerNames: [...drawerNames], items: [
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

// Inventory and reminders remain preview data; hardware only supplies door states.
let state = fixtures();
try {
  const saved = JSON.parse(localStorage.getItem(storageKey));
  if (saved?.version === 2 && Array.isArray(saved.devices) && saved.devices.length && saved.devices.every((device) => typeof device.id === 'string' && typeof device.name === 'string' && Array.isArray(device.items) && Array.isArray(device.reminders) && Array.isArray(device.activity) && device.open?.length === 3 && device.drawerNames?.length === 3)) {
    state = { ...state, ...saved };
  }
} catch { /* An unavailable or damaged local preview store must not block the UI. */ }
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
let hardware = { connected: false, state: null };
let camera = { connected: false, latest: null, cached_frames: 0 };
// The first preview cabinet is the single physical cabinet in this prototype.
const hardwareDeviceId = 'mb-01';

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
  cabinet?.update({ open: activeDevice().open, drawer: selectedDrawer, theme: resolvedTheme(), reduced: state.reducedMotion, instant });
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
function renderDock() {
  const device = activeDevice();
  $('drawer-dock').innerHTML = device.drawerNames.map((name, index) => `<button class="drawer-button ${selectedDrawer === index + 1 ? 'selected' : ''} ${device.open[index] ? 'open' : ''}" data-select-drawer="${index + 1}" aria-pressed="${selectedDrawer === index + 1}" aria-label="选择${ordinals[index]}：${esc(name)}"><span class="drawer-number">0${index + 1}</span><span><strong>${esc(name)}</strong><small>${device.open[index] ? '已打开' : '已合上'} · ${device.items.filter((item) => item.drawer === index + 1).length} 件</small></span></button>`).join('');
  const opened = device.open.flatMap((open, index) => open ? [ordinals[index]] : []);
  $('scene-state').innerHTML = `<span class="status-dot ${opened.length ? 'active' : ''}"></span><span>${opened.length ? opened.join('、') + '已打开' : '抽屉已合上'}</span>`;
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
  const dated = device.items.filter((item) => item.expiry).sort((a, b) => a.expiry.localeCompare(b.expiry));
  const candidate = dated[suggestionIndex % Math.max(dated.length, 1)];
  if (!candidate) return '';
  const days = Math.max(0, Math.floor((Date.now() - new Date(candidate.storedOn + 'T00:00:00').getTime()) / 86400000));
  return `<section class="panel-section"><div class="suggestion"><div class="suggestion-heading">${icon('sparkles')}一个小建议<button data-action="next-suggestion" aria-label="换一条建议" title="换一条建议">${icon('refresh-cw')}</button></div><p>${esc(candidate.name)}已经放了 ${days} 天，<br>下次可以先拿它。</p><small>${ordinals[candidate.drawer - 1]} · ${esc(candidate.name)} · 日期 ${monthDay(candidate.expiry)}</small></div></section>`;
}
function liveView() {
  const device = activeDevice();
  const opened = device.open.flatMap((open, index) => open ? [ordinals[index]] : []);
  if (!opened.length) return '';
  const latest = device.activity[0];
  const cameraView = camera.connected && hardware.connected && device.id === hardwareDeviceId
    ? '<img class="camera-live" src="/api/camera/mjpeg" alt="K230 实时摄像头画面">'
    : `<div class="camera-empty">${icon('camera-off')}<span>等待摄像头画面</span></div>`;
  return `<section class="live-view"><div class="live-heading"><span class="status-dot active"></span>${opened.join('、')}已打开<div class="inline-actions"><button class="icon-button small" data-action="demo-insert" aria-label="演示放入一件物品" data-tooltip="演示放入物品">${icon('package-plus')}</button></div></div>${cameraView}${latest?.demo ? `<div class="live-event">${icon('check-check')}<span>${esc(latest.title)} · 已更新预览</span></div><p class="live-meta">${esc(latest.time)} · ${ordinals[latest.drawer - 1]}</p>` : ''}</section>`;
}
function renderOverview() {
  const device = activeDevice();
  const reminders = device.reminders.filter((reminder) => !reminder.done).slice(0, 2);
  return `${liveView()}<section class="panel-section"><div class="section-title"><h3>抽屉</h3><span class="muted" style="font-size:10px">共 ${device.items.length} 件物品</span></div>${device.drawerNames.map((name, index) => `<button class="drawer-row" data-view-drawer="${index + 1}"><span class="row-index">0${index + 1}</span><span class="row-copy"><strong>${esc(name)}</strong><small>${device.items.filter((item) => item.drawer === index + 1).length} 件物品</small></span><span class="row-state ${device.open[index] ? 'open' : ''}"><span class="status-dot"></span>${device.open[index] ? '已打开' : '已合上'}</span>${icon('chevron-right')}</button>`).join('')}</section>${state.suggestions ? suggestion() : ''}<section class="panel-section"><div class="section-title"><h3>接下来的提醒</h3><button class="text-action" data-action="view-reminders">全部</button></div>${reminders.length ? reminders.map(reminderRow).join('') : '<p class="field-note">暂无提醒</p>'}</section><section class="panel-section"><div class="section-title"><h3>最近记录</h3></div>${device.activity.length ? device.activity.slice(0, 3).map((event) => `<div class="activity-row">${icon(event.direction === 'out' ? 'arrow-up-right' : 'arrow-down-left')}<span class="row-copy"><strong>${esc(event.title)}</strong><small>${ordinals[event.drawer - 1]}</small></span><span class="activity-time">${esc(event.time)}</span></div>`).join('') : '<p class="field-note">暂无记录</p>'}</section>`;
}
function itemRows() {
  const items = activeDevice().items.filter((item) => (!drawerFilter || item.drawer === drawerFilter) && item.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return items.length ? items.map((item) => `<div class="item-row"><button class="item-action" data-item="${esc(item.id)}"><span class="item-symbol">${icon(item.kind || 'package')}</span><span class="row-copy"><strong>${esc(item.name)}</strong><small>${ordinals[item.drawer - 1]} · ${monthDay(item.storedOn)} 放入${item.expiry ? `<br><span class="item-date">日期 ${monthDay(item.expiry)}</span>` : ''}</small></span>${icon('chevron-right')}</button></div>`).join('') : `<div class="empty-state">${icon('search')}${query ? '没有找到相关物品' : '这里还没有物品'}</div>`;
}
function renderItems() {
  return `<label class="search-field">${icon('search')}<input id="item-search" aria-label="搜索当前设备物品" placeholder="搜索物品" value="${esc(query)}" maxlength="80"></label><div class="filter-tabs" aria-label="按抽屉筛选">${['全部', '一层', '二层', '三层'].map((name, index) => `<button data-filter="${index}" class="${drawerFilter === index ? 'active' : ''}" aria-pressed="${drawerFilter === index}">${name}</button>`).join('')}</div><div id="item-results">${itemRows()}</div>`;
}
function renderReminders() {
  const reminders = activeDevice().reminders;
  function groupRows(list) {
    return list.map((reminder) => `<div class="reminder-detail-row ${reminder.done ? 'done' : ''}"><button class="reminder-check" data-complete="${esc(reminder.id)}" aria-label="${reminder.done ? '恢复' : '完成'}提醒：${esc(reminder.title)}">${reminder.done ? icon('check') : ''}</button><button class="reminder-edit" data-reminder="${esc(reminder.id)}"><span class="row-copy"><strong>${esc(reminder.title)}</strong><small>${esc(reminderMeta(reminder))}</small></span></button><button class="icon-button small" data-reminder="${esc(reminder.id)}" aria-label="编辑提醒：${esc(reminder.title)}">${icon('chevron-right')}</button></div>`).join('');
  }
  const pending = reminders.filter((reminder) => !reminder.done);
  const done = reminders.filter((reminder) => reminder.done);
  return `<div class="section-title"><h3>提醒与任务</h3><button class="icon-button small" data-action="add-reminder" aria-label="添加提醒" data-tooltip="添加提醒">${icon('plus')}</button></div>${pending.length ? groupRows(pending) : `<div class="empty-state">${icon('bell')}暂无待办提醒</div>`}${done.length ? `<div class="reminder-group-label">已完成</div>${groupRows(done)}` : ''}`;
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
  $('hardware-status').textContent = linked ? (hardware.state?.source === 'simulation' ? '模拟设备已连接' : 'K230 已连接') : '设备未连接';
  $('demo-drawer').disabled = linked;
}
function applyDoorSnapshot(snapshot) {
  if (snapshot?.schema !== 1 || snapshot.cabinet_id !== 'cabinet-01' || typeof snapshot.connected !== 'boolean') return;
  const message = snapshot.state;
  if (message && (!Array.isArray(message.open) || message.open.length !== 3 || message.open.some((value) => typeof value !== 'boolean'))) return;
  const connectionChanged = hardware.connected !== snapshot.connected;
  hardware = snapshot;
  const device = state.devices.find((entry) => entry.id === hardwareDeviceId);
  if (device && message && snapshot.connected) {
    const changed = device.open.some((value, index) => value !== message.open[index]);
    device.open = [...message.open];
    if (device.id === activeId && changed) {
      if (Number.isInteger(message.drawer) && message.drawer >= 1 && message.drawer <= 3) selectedDrawer = message.drawer;
      renderDock();
      syncModel();
    }
    if (device.id === activeId && activeTab === 'overview' && (changed || connectionChanged)) renderPanel();
  } else if (connectionChanged && activeDevice().id === hardwareDeviceId && activeTab === 'overview') {
    renderPanel();
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
  activeId = id;
  selectedDrawer = 1;
  drawerFilter = 0;
  query = '';
  $('command-response').hidden = true;
  render(true);
}
function toggleDrawer() {
  const device = activeDevice();
  if (device.id === hardwareDeviceId && hardware.connected) return;
  device.open[selectedDrawer - 1] = !device.open[selectedDrawer - 1];
  if (activeTab !== 'overview') activeTab = 'overview';
  render(); persist();
  if (panelCollapsed) document.querySelector('.activity-dot').hidden = true;
}
function demoInsert() {
  const device = activeDevice();
  const drawer = device.open[selectedDrawer - 1] ? selectedDrawer : device.open.findIndex(Boolean) + 1;
  if (!drawer) { toast('先在演示中打开一个抽屉'); return; }
  const item = makeItem(uid(), '燕麦能量棒', drawer, 'cookie', 0, localDate(30), '', '这个保质期是下个月今天。');
  device.items.unshift(item);
  device.activity.unshift({ title: '放入燕麦能量棒', drawer, time: timeNow(), direction: 'in', demo: true });
  render(); persist();
  if (panelCollapsed) document.querySelector('.activity-dot').hidden = false;
}
function completeReminder(id) {
  const reminder = activeDevice().reminders.find((entry) => entry.id === id);
  if (!reminder) return;
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
  openModal('物品详情', `<form id="item-form" class="form-grid"><label class="field">名称<input name="name" value="${esc(item.name)}" maxlength="48" required></label><div class="field-row"><label class="field">所在抽屉<select name="drawer">${ordinals.map((name, index) => `<option value="${index + 1}" ${item.drawer === index + 1 ? 'selected' : ''}>${name}</option>`).join('')}</select></label><label class="field">到期日期<input name="expiry" type="date" value="${esc(item.expiry)}"></label></div><label class="field">备注<textarea name="note" maxlength="200">${esc(item.note)}</textarea></label>${item.voice ? `<div class="source-quote"><span>随物品记录的原话</span>“${esc(item.voice)}”</div>` : ''}<div class="modal-actions"><button class="button danger" type="button" id="remove-item">${icon('arrow-up-right')}标记已取出</button>${cancelButton}<button class="button primary" type="submit">保存</button></div></form>`);
  $('item-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const name = String(form.get('name')).trim();
    if (!name) { event.currentTarget.elements.name.setCustomValidity('请填写物品名称'); event.currentTarget.reportValidity(); return; }
    Object.assign(item, { name, drawer: Number(form.get('drawer')), expiry: String(form.get('expiry')), note: String(form.get('note')).trim() });
    persist(); closeModal(); render(); toast('已保存到本地预览');
  });
  $('remove-item').addEventListener('click', () => {
    device.items = device.items.filter((entry) => entry.id !== id);
    device.activity.unshift({ title: '取出' + item.name, drawer: item.drawer, direction: 'out', time: timeNow() });
    persist(); closeModal(); render(); toast('已在预览中标记取出');
  });
}
function openReminder(id = '', preset = {}) {
  const device = activeDevice();
  const existing = device.reminders.find((entry) => entry.id === id);
  const reminder = existing || { title: '', date: localDate(), time: '', repeat: 'once', ...preset };
  openModal(existing ? '编辑提醒' : '添加提醒', `<form id="reminder-form" class="form-grid"><label class="field">提醒内容<input name="title" value="${esc(reminder.title)}" maxlength="80" required placeholder="吃鱼油"></label><div class="field-row"><label class="field">日期<input name="date" type="date" value="${esc(reminder.date)}" required></label><label class="field">时间<input name="time" type="time" value="${esc(reminder.time)}" required></label></div><label class="field">重复<select name="repeat"><option value="once" ${reminder.repeat === 'once' ? 'selected' : ''}>仅一次</option><option value="daily" ${reminder.repeat === 'daily' ? 'selected' : ''}>每天</option></select></label><p class="field-note">保存在本地预览，尚未连接设备提醒服务。</p><div class="modal-actions">${existing ? `<button class="button danger" type="button" id="delete-reminder">${icon('trash-2')}删除</button>` : ''}${cancelButton}<button class="button primary" type="submit">保存</button></div></form>`);
  $('reminder-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const title = String(form.get('title')).trim();
    if (!title) { event.currentTarget.elements.title.setCustomValidity('请填写提醒内容'); event.currentTarget.reportValidity(); return; }
    const values = { title, date: String(form.get('date')), time: String(form.get('time')), repeat: String(form.get('repeat')) };
    if (existing) Object.assign(existing, values);
    else device.reminders.push({ ...values, id: uid(), done: false });
    persist(); closeModal(); activeTab = 'reminders'; setPanel(false); render(); toast('提醒已保存到本地预览');
  });
  $('delete-reminder')?.addEventListener('click', () => {
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
  openModal('设置', `<div class="setting-row"><span>外观</span><div class="theme-switch" aria-label="主题">${[['light', 'sun', '浅色'], ['dark', 'moon', '深色'], ['system', 'monitor', '跟随系统']].map(([value, symbol, label]) => `<button data-theme-choice="${value}" class="${state.theme === value ? 'selected' : ''}" aria-label="${label}" aria-pressed="${state.theme === value}" title="${label}">${icon(symbol)}</button>`).join('')}</div></div><div class="setting-row"><span>物品建议</span><label class="toggle"><input type="checkbox" id="suggestions-toggle" aria-label="物品建议" ${state.suggestions ? 'checked' : ''}><span></span></label></div><div class="setting-row"><span>减少动态效果</span><label class="toggle"><input type="checkbox" id="motion-toggle" aria-label="减少动态效果" ${state.reducedMotion ? 'checked' : ''}><span></span></label></div><div class="demo-settings"><h3>预览控制</h3><div class="demo-actions"><button class="button" data-action="demo-drawer">${icon('play')}抽屉开合</button><button class="button" data-action="demo-insert">${icon('package-plus')}放入物品</button></div></div>`);
  $('suggestions-toggle').addEventListener('change', (event) => { state.suggestions = event.target.checked; persist(); renderPanel(); });
  $('motion-toggle').addEventListener('change', (event) => { state.reducedMotion = event.target.checked; persist(); syncModel(); });
}
function commandOpen() {
  $('command-panel').hidden = false;
  $('command-input').focus();
}
function answer(text) { $('command-response').textContent = text; $('command-response').hidden = false; }
function submitCommand(event) {
  event.preventDefault();
  const raw = $('command-input').value.trim();
  if (!raw) return;
  if (raw.includes('提醒')) {
    const title = raw.split('提醒我')[1]?.replace(/[。！!，,]/g, '').trim() || '';
    answer('请补充或核对提醒时间，保存后会出现在预览列表。');
    openReminder('', { title, repeat: raw.includes('每天') ? 'daily' : 'once' });
    return;
  }
  const term = raw.replace(/[？?。！!，,]/g, '').replace(/请问|帮我找一下|帮我找找|帮我找|我想找|我的|在哪里|在哪儿|在哪|放哪了|放在哪里了|放在哪里|找一下|找找/g, '').trim();
  if (!term) { answer('请输入要找的物品名称。'); return; }
  const matches = state.devices.flatMap((device) => device.items.filter((item) => item.name.includes(term)).map((item) => ({ device, item })));
  if (!matches.length) { answer('当前预览记录里没有找到“' + term + '”。'); return; }
  const { device, item } = matches[0];
  switchDevice(device.id);
  selectedDrawer = item.drawer;
  drawerFilter = item.drawer;
  query = item.name;
  activeTab = 'items';
  setPanel(false); render();
  answer(matches.map((match) => `${match.item.name}在${match.device.name}的${ordinals[match.item.drawer - 1]}。`).join('\n'));
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.device) switchDevice(button.dataset.device);
  if (button.dataset.selectDrawer) selectDrawer(Number(button.dataset.selectDrawer));
  if (button.dataset.viewDrawer) { selectDrawer(Number(button.dataset.viewDrawer)); drawerFilter = selectedDrawer; query = ''; setTab('items'); }
  if (button.dataset.tab) setTab(button.dataset.tab);
  if (button.dataset.filter != null) { drawerFilter = Number(button.dataset.filter); if (drawerFilter) selectDrawer(drawerFilter); renderPanel(); }
  if (button.dataset.item) openItem(button.dataset.item);
  if (button.dataset.reminder) openReminder(button.dataset.reminder);
  if (button.dataset.complete) completeReminder(button.dataset.complete);
  if (button.dataset.themeChoice) { state.theme = button.dataset.themeChoice; persist(); applyTheme(); settings(); }
  const actions = {
    'next-suggestion': () => { suggestionIndex++; renderPanel(); },
    'view-reminders': () => setTab('reminders'),
    'add-reminder': () => openReminder(),
    'close-modal': closeModal,
    'demo-drawer': () => { closeModal(); toggleDrawer(); },
    'demo-insert': () => { closeModal(); demoInsert(); },
  };
  actions[button.dataset.action]?.();
});
document.addEventListener('input', (event) => {
  if (event.target.id === 'item-search') { query = event.target.value; $('item-results').innerHTML = itemRows(); paintIcons(); }
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
$('reset-view').addEventListener('click', () => cabinet?.resetView());
$('command-button').addEventListener('click', commandOpen);
$('close-command').addEventListener('click', () => { $('command-panel').hidden = true; $('command-button').focus(); });
$('command-form').addEventListener('submit', submitCommand);
$('close-modal').addEventListener('click', closeModal);
$('modal').addEventListener('click', (event) => { if (event.target === $('modal')) { const box = $('modal').getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeModal(); } });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !$('modal').open) $('command-panel').hidden = true; });
systemDark.addEventListener('change', () => { if (state.theme === 'system') applyTheme(); });
mobile.addEventListener('change', (event) => setPanel(event.matches));
function updateClock() { $('clock').textContent = new Date().toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) + '  ' + timeNow(); }
updateClock(); setInterval(updateClock, 30000);
applyTheme(); setPanel(panelCollapsed); render();
let doorEvents;
function disconnectDoorEvents() {
  const wasConnected = hardware.connected;
  hardware = { ...hardware, connected: false };
  renderHardwareStatus();
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
import('./cabinet.js').then(({ createCabinet }) => {
  cabinet = createCabinet($('viewport'), selectDrawer);
  syncModel(true);
}).catch((error) => {
  $('model-fallback').hidden = false;
  console.warn('Cabinet renderer unavailable:', error);
});

// Read-only diagnostics for browser verification, with no production inventory writes.
window.mindBoxPreview = { snapshot: () => ({ device: activeId, selectedDrawer, open: [...activeDevice().open], panelCollapsed, theme: resolvedTheme(), hardware, camera, model: cabinet?.snapshot() }) };
