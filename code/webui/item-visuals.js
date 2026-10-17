// Sample photos are curated assets, not the output of live recognition.
export const samplePhotos = {
  cola: { thumbnailUrl: '/items/cola.webp', originalUrl: '/items/capture-v10.webp', sourceLabel: '测试视频截图 · 样例', mode: 'cutout' },
  chicken: { thumbnailUrl: '/items/chicken.webp', originalUrl: '/items/capture-v10.webp', sourceLabel: '测试视频截图 · 样例', mode: 'cutout' },
  tea: { thumbnailUrl: '/items/tea.webp', originalUrl: '/items/capture-v8.webp', sourceLabel: '测试视频截图 · 样例', mode: 'cutout' },
};

const shapes = new Set(['packet', 'box', 'bottle', 'can', 'cable', 'flat']);
export function itemShape(item) {
  if (shapes.has(item.shape)) return item.shape;
  if (['cable', 'headphones'].includes(item.kind)) return 'cable';
  if (['pill', 'milk'].includes(item.kind)) return 'bottle';
  if (item.kind === 'cup-soda') return 'can';
  if (['cookie', 'coffee'].includes(item.kind)) return 'packet';
  if (['notebook-pen', 'battery-charging', 'smartphone'].includes(item.kind)) return 'flat';
  return 'box';
}
