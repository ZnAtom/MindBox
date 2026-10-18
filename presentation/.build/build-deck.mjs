import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

process.env.RUNTIME_NODE_MODULES = '/Users/cao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';

const WORK = '/Users/cao/MindBox/presentation';
const BUILD = path.join(WORK, '.build');
const ASSETS = path.join(BUILD, 'assets');
const SKILL = '/Users/cao/.codex/plugins/cache/openai-primary-runtime/presentations/26.1013.11914/skills/presentations';
const PYTHON = '/Users/cao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const FINAL = path.join(WORK, 'output', 'MindBox_Keynote.pptx');
const CN = 'Hiragino Sans GB';
const EN = 'Helvetica Neue';
const C = { black: '#151515', white: '#FFFFFF', gray: '#747474', light: '#F2F2F2', blue: '#0071E3' };
const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
presentation.theme.colorScheme = {
  name: 'Mind Box',
  themeColors: {
    accent1: C.blue, accent2: '#467C65', accent3: '#B2403F', accent4: '#A67925',
    accent5: '#797979', accent6: '#3D676C', bg1: C.white, bg2: C.light,
    tx1: C.black, tx2: C.gray, dk1: '#000000', dk2: C.black,
    lt1: C.white, lt2: C.light, hlink: C.blue, folHlink: '#555555',
  },
};

function slide(background = C.white) {
  const s = presentation.slides.add();
  s.background.fill = background;
  return s;
}

function text(s, name, value, x, y, width, height, size, options = {}) {
  const shape = s.shapes.add({
    geometry: 'textbox', name,
    position: { left: x, top: y, width, height },
    fill: 'none', line: { fill: 'none', width: 0 },
  });
  shape.text = value;
  shape.text.style = {
    fontSize: size, typeface: options.font || CN,
    color: options.color || C.black, bold: options.bold ?? false,
    alignment: options.align || 'left', verticalAlignment: 'middle',
    wrap: 'none', autoFit: 'none', insets: 0,
  };
  return shape;
}

async function image(s, name, x, y, width, height, fit = 'contain') {
  const buffer = await fs.readFile(path.join(ASSETS, name));
  return s.images.add({
    blob: new Uint8Array(buffer), contentType: name.endsWith('.png') ? 'image/png' : 'image/jpeg',
    alt: name.replace(/[-.]/g, ' '), fit,
    position: { left: x, top: y, width, height },
  });
}

function notes(s, timing, script, sources = []) {
  s.speakerNotes.text = `${timing}\n\n${script}\n\n来源\n${sources.join('\n')}`;
}

// Opening frame is also the holding slide for the separate two-minute demonstration.
{
  const s = slide(C.light);
  await image(s, 'cabinet-hero.png', 574, 74, 676, 600);
  text(s, 'brand', 'Mind Box', 82, 236, 600, 120, 104, { font: EN, bold: true });
  text(s, 'category', '物品无感智能收纳', 88, 369, 480, 56, 34);
  notes(s, '视频与演示约 120 秒',
    '这是我们的产品，Mind Box。物品放取的过程，自动记录；想找物品的时候，定位到所在抽屉。请看演示。\n\n演示：取出物品、存入物品，同时交代提醒。随后展示语音查找与提醒。',
    ['产品定义：用户提供的演讲稿。', '产品画面：项目 WebUI 原有 3D 柜体模型截图，非实物照片。',
     '/Users/cao/MindBox/code/webui/cabinet.js', '视觉参考：https://www.apple.com/apple-events/']);
}

{
  const s = slide();
  text(s, 'remember', '取放，全程记得', 78, 202, 500, 84, 54, { bold: true });
  text(s, 'find', '想找，一问就知道', 78, 286, 500, 84, 54, { bold: true });
  text(s, 'feature-summary', '自动记录取放', 82, 433, 420, 47, 28, { color: C.gray });
  text(s, 'voice-reminders', '语音查找与定时提醒', 82, 480, 420, 47, 28, { color: C.gray });
  await image(s, 'interface.png', 591, 140, 642, 402);
  notes(s, '讲述 00:00–00:12，约 12 秒',
    '刚才大家看到的，就是 Mind Box。物品放入、取出，系统自动留下记录。需要的时候，可以问它物品在哪里，也可以直接交代一条提醒。',
    ['/Users/cao/MindBox/code/backend/app/automatic.py', '/Users/cao/MindBox/code/backend/app/voice_api.py',
     '/Users/cao/MindBox/code/backend/app/live_tasks.py', '/Users/cao/MindBox/code/webui/README.md',
     '截图为本地实际 WebUI，保留当时页面状态。代码已包含这些功能；文档仍将实柜音画联合验收列为待完成事项。']);
}

{
  const s = slide();
  await image(s, 'everyday-objects.jpg', 0, 0, 1280, 720, 'cover');
  text(s, 'open-world-1', '生活里的物品', 248, 227, 900, 84, 68, { bold: true });
  text(s, 'open-world-2', '无需逐一定义', 248, 318, 900, 84, 68, { bold: true });
  text(s, 'multimodal', '多模态 AI，理解日常物品', 252, 431, 830, 47, 30, { color: '#5B6163' });
  notes(s, '讲述 00:12–00:34，约 22 秒',
    '这个项目，来自我对未来万物互联生活的想象。过去，我们需要提前定义程序的规则；而今天，多模态 AI 让我们有机会理解更多没有逐一定义过的物品和场景。日常生活里的物品千差万别，我们希望，收纳也能跟上这样的变化。',
    ['“无需逐一定义”指不必为每个物品类别单独编写识别规则，不代表所有物品均已验证识别成功。',
     '/Users/cao/MindBox/code/backend/app/prompts/causal-v1.txt', '/Users/cao/MindBox/code/README.md',
     '场景配图：Jess Bailey / Unsplash，Unsplash License。',
     'https://unsplash.com/photos/desk-flat-lay-with-stationery-and-macaron-9byKncZaV0c',
     '图片表达生活物品，并非本项目识别结果。']);
}

{
  const s = slide();
  text(s, 'agent-title', '让个人 Agent，接上你的生活', 76, 87, 1130, 88, 60, { bold: true });
  text(s, 'mindbox-side', 'Mind Box', 133, 292, 450, 112, 76, { font: EN, bold: true });
  text(s, 'connection-symbol', '×', 609, 303, 90, 80, 60, { font: EN, color: '#A1A1A1', align: 'center' });
  text(s, 'agent-side', '个人 Agent', 773, 303, 410, 90, 56, { bold: true });
  text(s, 'mindbox-facts', '物品、位置、取放记录', 138, 421, 460, 46, 27, { color: C.gray });
  text(s, 'agent-role', '你的生活管家', 780, 421, 390, 46, 27, { color: C.gray });
  text(s, 'authenticated-api', '经认证的 API 接入', 76, 607, 1100, 46, 28, { align: 'center', color: C.blue });
  notes(s, '讲述 00:34–00:50，约 16 秒',
    '所以，我们也完成了带认证的 API。个人 Agent 获得授权后，就能读取物品、位置和取放记录。未来，像 Muse 这样的个人 Agent，也可以接进来，成为了解我们生活的管家。',
    ['/Users/cao/MindBox/code/backend/app/agent_server.py', '/Users/cao/MindBox/code/backend/app/api_keys.py',
     '/Users/cao/MindBox/code/backend/tests/test_agent_api.py', '/Users/cao/MindBox/code/doc/agent-api-plan.md',
     '现有实现为本机带认证的只读查询接口。Muse 是未来接入举例，项目材料未证明已完成 Muse 客户端验收。']);
}

{
  const s = slide('#080808');
  text(s, 'many-items', '物品越来越多', 115, 215, 1050, 110, 88, { bold: true, color: '#9A9A9A', align: 'center' });
  text(s, 'limited-attention', '注意力只有这么多', 80, 348, 1120, 110, 88, { bold: true, color: C.white, align: 'center' });
  notes(s, '讲述 00:50–01:04，约 14 秒',
    'Mind Box 想解决的，是一个很日常的矛盾：人的注意力有限，要管理的物品却越来越多。我们总会把注意力放在重要的事情上，而收纳，也不该持续占用这份宝贵的注意力。',
    ['价值主张：用户提供的演讲稿。不作为量化研究或统计结论。']);
}

{
  const s = slide();
  text(s, 'organized-label', '善于收纳', 114, 110, 700, 56, 33, { color: C.gray });
  text(s, 'organized-benefit', '更有条理', 110, 175, 800, 108, 82, { bold: true });
  text(s, 'unorganized-label', '不善收纳', 114, 369, 700, 56, 33, { color: C.gray });
  text(s, 'unorganized-benefit', '也能找得到', 110, 434, 800, 108, 82, { bold: true, color: C.blue });
  await image(s, 'cabinet-photo-left.png', 907, 145, 300, 452);
  notes(s, '讲述 01:04–01:17，约 13 秒',
    '如果你善于收纳，Mind Box 帮你更有条理，把精力留给重要的事。如果你不善收纳，那更欢迎你，因为它就是为我们这样的人打造的。',
    ['用户定位：用户提供的演讲稿。',
     '项目柜体参考实拍：/Users/cao/MindBox/code/webui/public/models/reference-03-FL.webp',
     '原图来源说明：/Users/cao/MindBox/code/webui/public/models/SOURCES.md']);
}

{
  const s = slide('#080808');
  text(s, 'delegate-1', '把繁杂的', 80, 218, 1120, 112, 94, { bold: true, color: C.white, align: 'center' });
  text(s, 'delegate-2', '交给 AI', 80, 358, 1120, 112, 94, { bold: true, color: C.white, align: 'center' });
  notes(s, '讲述 01:17–01:22，约 5 秒', '让我们把繁杂的，交给 AI。', ['用户提供的演讲稿。']);
}

{
  const s = slide();
  text(s, 'human-values', '把情感、创造和生活', 80, 58, 1120, 82, 61, { bold: true, align: 'center' });
  text(s, 'human-close', '留给人类', 80, 146, 1120, 93, 74, { bold: true, align: 'center' });
  text(s, 'brand-close', 'Mind Box', 80, 263, 1120, 52, 33, { font: EN, bold: true, align: 'center' });
  await image(s, 'creativity-wide.jpg', 0, 360, 1280, 360, 'cover');
  notes(s, '讲述 01:22–01:30，约 8 秒',
    '把情感、创造和生活，留给人类。Mind Box，谢谢大家。',
    ['用户提供的演讲稿。', '生活配图：Sebastian Monroy / Unsplash，Unsplash License。',
     'https://unsplash.com/photos/hands-shaping-a-clay-pot-with-pottery-tools-OloJS1Eo0B4',
     '图片表达人的创造活动，并非团队工作现场。']);
}

await fs.mkdir(path.join(BUILD, 'previews'), { recursive: true });
await fs.mkdir(path.join(WORK, 'output'), { recursive: true });
for (const [i, s] of presentation.slides.items.entries()) {
  const name = 'slide-' + String(i + 1).padStart(2, '0');
  const png = await presentation.export({ slide: s, format: 'png', scale: 1.5 });
  await fs.writeFile(path.join(BUILD, 'previews', name + '.png'), new Uint8Array(await png.arrayBuffer()));
  const layout = await s.export({ format: 'layout' });
  await fs.writeFile(path.join(BUILD, 'previews', name + '.json'), await layout.text());
  console.log('Rendered', name);
}

const montage = await presentation.export({ format: 'webp', montage: true });
await fs.writeFile(path.join(BUILD, 'overview.webp'), new Uint8Array(await montage.arrayBuffer()));
await fs.writeFile(path.join(BUILD, 'presentation.json'), JSON.stringify(presentation.toProto()));
const candidate = path.join(BUILD, 'candidate.pptx');
await (await PresentationFile.exportPptx(presentation)).save(candidate);
const { finalizePresentation } = await import(pathToFileURL(path.join(SKILL, 'container_tools/artifact_tool_utils.mjs')).href);
const result = await finalizePresentation({
  workspaceDir: WORK, candidatePath: candidate, finalPath: FINAL,
  pythonExecutable: PYTHON,
  integrityValidatorPath: path.join(SKILL, 'container_tools/inspect_presentation_package_integrity.py'),
  layoutValidatorPath: path.join(SKILL, 'container_tools/inspect_presentation_layout_geometry.py'),
  layoutArgs: ['--expected-slide-size-emu', '12192000,6858000', '--validate-bullet-geometry', '--validate-heading-fit'],
  requiredNativeTableOwnerSlides: [],
  fontPolicy: { basis: 'design', families: [CN, EN], scriptFonts: { ea: CN } },
  verifyArtifactToolImport: true,
  receiptPath: path.join(BUILD, 'validation.json'),
});
console.log('Finalized:', JSON.stringify(result));
