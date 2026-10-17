import './vision.css';
import { createIcons, ArrowLeft, RefreshCw, Printer } from 'lucide';

createIcons({ icons: { ArrowLeft, RefreshCw, Printer } });
const $ = (id) => document.getElementById(id);
const names = ['上层 · 1', '中层 · 2', '下层 · 3'];
const labels = { disabled: '视觉模式未启用', no_camera: '等待摄像头', tracking: '识别正常', initializing: '正在确认状态', opening_pending: '正在确认开门', closing_pending: '正在确认关门', multiple_markers: '多个识别码冲突', error: '识别异常' };
let latest = { enabled: false };
let loading = false;
let timer;
let overlayTimer;
let streamEpoch;
let streamFailed = false;
let retryAt = 0;
const context = $('overlay').getContext('2d');

function render() {
  $('status').textContent = labels[latest.status] || '连接不可用';
  $('status').dataset.state = latest.status || '';
  $('resolution').textContent = latest.image_size ? `${latest.image_size.join(' × ')} · 视频 ${latest.camera_fps || 0} fps · 识别 ${latest.processing_fps || 0} fps` : '—';
  const opened = latest.open?.findIndex(Boolean) ?? -1;
  $('door-status').textContent = latest.known?.every(Boolean) ? opened >= 0 ? `${names[opened].split(' · ')[0]}已打开` : '全部已关闭' : '状态待确认';
  $('open-delay').textContent = latest.open_confirm_seconds == null ? '—' : `${latest.open_confirm_seconds} 秒`;
  $('close-delay').textContent = latest.close_delay_seconds == null ? '—' : `${latest.close_delay_seconds} 秒`;
  $('message').textContent = latest.error || (latest.status === 'multiple_markers' ? '同时检测到多个抽屉标记' : latest.pending_remaining_ms > 0 ? `${labels[latest.status]} · ${(latest.pending_remaining_ms / 1000).toFixed(1)} 秒` : '');
  $('markers').innerHTML = names.map((name, index) => {
    const marker = latest.markers?.find((entry) => entry.id === index + 1);
    const known = latest.known?.[index] === true;
    const status = !known ? '待确认' : latest.open[index] ? '已打开' : '已合上';
    return `<tr><td>${name}</td><td data-known="${!!marker?.visible}">${marker?.visible ? '可见' : '未识别'}</td><td data-known="${known}">${status}</td></tr>`;
  }).join('');
  const printable = [...document.querySelectorAll('[data-marker]')];
  for (const image of printable) if (latest.enabled && !image.getAttribute('src')) image.src = `/api/vision/markers/${image.dataset.marker}.png`;
  $('print').disabled = !latest.enabled || printable.some((image) => !image.complete || !image.naturalWidth);
}

function clearOverlay() {
  clearTimeout(overlayTimer);
  context.clearRect(0, 0, $('overlay').width, $('overlay').height);
}

function drawOverlay() {
  clearOverlay();
  if (!latest.camera_connected || !latest.image_size || latest.observation_age_ms == null || latest.observation_age_ms > 500) return;
  const [width, height] = latest.image_size;
  const canvas = $('overlay');
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  context.strokeStyle = '#3cb432';
  context.fillStyle = '#206e20';
  context.lineWidth = 2;
  context.font = '20px sans-serif';
  for (const marker of latest.markers || []) {
    if (!marker.visible || !marker.corners) continue;
    context.beginPath();
    marker.corners.forEach(([x, y], index) => index ? context.lineTo(x, y) : context.moveTo(x, y));
    context.closePath();
    context.stroke();
    context.fillText(String(marker.id), marker.corners[0][0], Math.max(22, marker.corners[0][1] - 7));
  }
  overlayTimer = setTimeout(clearOverlay, 500 - latest.observation_age_ms);
}

function updatePreview() {
  const image = $('preview');
  if (!latest.enabled || !latest.camera_connected) {
    image.removeAttribute('src');
    streamEpoch = undefined;
    $('preview').hidden = true;
    $('empty').hidden = false;
    clearOverlay();
    return;
  }
  if (streamFailed && performance.now() < retryAt) return;
  if (!image.getAttribute('src') || streamEpoch !== latest.camera_epoch || streamFailed) {
    streamFailed = false;
    streamEpoch = latest.camera_epoch;
    image.src = '/api/camera/mjpeg?t=' + Date.now();
  }
  image.hidden = false;
  $('empty').hidden = true;
  drawOverlay();
}
$('preview').addEventListener('error', () => {
  streamFailed = true;
  retryAt = performance.now() + 1000;
  $('preview').hidden = true;
  $('empty').hidden = false;
  clearOverlay();
});

async function poll() {
  if (loading) return;
  loading = true;
  clearTimeout(timer);
  try {
    const response = await fetch('/api/vision/state', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    if (!response.ok) throw new Error('视觉服务暂不可用');
    latest = await response.json();
    render();
    updatePreview();
  } catch {
    latest = { enabled: false, status: 'unavailable' };
    render();
    updatePreview();
  } finally {
    loading = false;
    timer = setTimeout(poll, 200);
  }
}
$('refresh').addEventListener('click', poll);
$('print').addEventListener('click', () => window.print());
render();
poll();
