import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import sharp from 'sharp';
import { GlobalFonts } from '@napi-rs/canvas';

const dir = '/Users/cao/MindBox/presentation/.build/assets';
await fs.mkdir(dir, { recursive: true });
console.log('Fonts:', GlobalFonts.families.filter(f => /PingFang|Hiragino Sans GB|Helvetica Neue|Heiti|Noto Sans CJK/.test(f.family)));

const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });
  await page.goto('http://127.0.0.1:5173/', { waitUntil: 'domcontentloaded' });
  await page.locator('#viewport canvas').waitFor();
  await page.waitForFunction(() => window.mindBoxPreview?.snapshot()?.model);
  await page.screenshot({ path: path.join(dir, 'interface-real.png') });
  console.log('Title:', await page.title());
  console.log('State:', JSON.stringify(await page.evaluate(() => {
    const s = window.mindBoxPreview.snapshot();
    return { device: s.device, itemCount: s.itemCount, model: s.model };
  })));
  await page.locator('#viewport canvas').screenshot({ path: path.join(dir, 'cabinet-canvas.png') });
  await page.locator('#settings-button').click();
  await page.screenshot({ path: path.join(dir, 'settings.png') });
  await page.locator('#close-modal').click();
  await page.locator('#command-button').click();
  await page.locator('#command-input').fill('可乐放在哪里？');
  await page.screenshot({ path: path.join(dir, 'voice-interface.png') });
  console.log('Canvas screenshot and product interface captured');
} finally { await browser.close(); }

// Remove only the connected neutral backdrop from the existing product photograph.
for (const [file, target] of [['reference-02-FR.webp', 'cabinet-photo-right.png'], ['reference-03-FL.webp', 'cabinet-photo-left.png']]) {
  const { data, info } = await sharp('/Users/cao/MindBox/code/webui/public/models/' + file).ensureAlpha().raw().toBuffer({ resolveWithObject: true });
  const seen = new Uint8Array(info.width * info.height);
  const queue = [];
  function add(x, y) {
    if (x < 0 || y < 0 || x >= info.width || y >= info.height) return;
    const i = y * info.width + x;
    if (seen[i]) return;
    seen[i] = 1;
    const j = i * 4;
    if (Math.abs(data[j] - 239) > 10 || Math.abs(data[j + 1] - 239) > 10 || Math.abs(data[j + 2] - 239) > 10) return;
    data[j + 3] = 0;
    queue.push([x, y]);
  }
  for (let x = 0; x < info.width; x++) { add(x, 0); add(x, info.height - 1); }
  for (let y = 0; y < info.height; y++) { add(0, y); add(info.width - 1, y); }
  for (let q = 0; q < queue.length; q++) {
    const [x, y] = queue[q];
    add(x - 1, y); add(x + 1, y); add(x, y - 1); add(x, y + 1);
  }
  await sharp(data, { raw: info }).trim().png().toFile(path.join(dir, target));
}

const photos = [
  ['human-creativity.jpg', 'https://images.unsplash.com/photo-1781389004999-d1a7e41639c5?auto=format&fit=crop&fm=jpg&q=90&w=2400'],
];
for (const [name, url] of photos) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} while downloading ${name}`);
  const buffer = Buffer.from(await response.arrayBuffer());
  await fs.writeFile(path.join(dir, name), buffer);
  console.log(name, await sharp(buffer).metadata());
}
