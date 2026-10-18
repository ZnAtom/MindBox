import fs from 'node:fs/promises';
import sharp from 'sharp';

const dir = '/Users/cao/MindBox/presentation/.build/assets';
const url = 'https://images.unsplash.com/photo-1764818958908-d5efcec563d1?auto=format&fit=crop&fm=jpg&q=90&w=2400';
const response = await fetch(url);
if (!response.ok) throw new Error('Could not download stationery photograph: ' + response.status);
await fs.writeFile(dir + '/everyday-objects.jpg', Buffer.from(await response.arrayBuffer()));
console.log('Everyday objects:', await sharp(dir + '/everyday-objects.jpg').metadata());
await sharp(dir + '/cabinet-canvas.png')
  .extract({ left: 800, top: 170, width: 1150, height: 1020 })
  .png().toFile(dir + '/cabinet-hero.png');
const pixel = await sharp(dir + '/cabinet-canvas.png').extract({ left: 0, top: 200, width: 1, height: 1 }).raw().toBuffer();
console.log('Canvas background pixel:', Array.from(pixel));
await sharp(dir + '/interface-real.png').resize({ width: 2400 }).png().toFile(dir + '/interface.png');
await sharp(dir + '/human-creativity.jpg').extract({ left: 0, top: 1050, width: 2400, height: 1350 })
  .jpeg({ quality: 95 }).toFile(dir + '/creativity-wide.jpg');
