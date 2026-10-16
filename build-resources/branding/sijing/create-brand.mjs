import sharp from 'sharp';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const output = dirname(fileURLToPath(import.meta.url));
await mkdir(output, { recursive: true });
const route = 'M708 272H458C365 272 310 324 310 394C310 461 359 499 443 514L586 540C665 554 713 589 713 648C713 714 659 752 570 752H422';
const mark = color => `<path d="${route}" fill="none" stroke="${color}" stroke-width="76" stroke-linecap="round" stroke-linejoin="round"/><circle cx="308" cy="752" r="40" fill="${color}"/>`;
const svg = body => `<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">${body}</svg>`;
const themes = {
  dark: { background: '#28282c', foreground: '#f7f7f9', edge: '#45454a' },
  light: { background: '#f5f5f7', foreground: '#29292e', edge: '#e0e0e5' },
};
const tile = theme => `<rect x="48" y="48" width="928" height="928" rx="220" fill="${theme.background}" stroke="${theme.edge}" stroke-width="2"/>${mark(theme.foreground)}`;
for (const [name, theme] of Object.entries(themes)) {
  const source = svg(tile(theme));
  await writeFile(resolve(output, `icon-${name}.svg`), source);
  for (const size of [16, 24, 32, 48, 64, 128, 256, 512, 1024]) {
    await sharp(Buffer.from(source)).resize(size, size).png().toFile(resolve(output, `icon-${name}-${size}.png`));
  }
  await writeFile(resolve(output, `mark-${name}.svg`), svg(mark(theme.foreground)));
}
await writeFile(resolve(output, 'mark.svg'), svg(mark('currentColor')));

const sizes = [16, 24, 32, 48, 64, 128, 256];
const frames = await Promise.all(sizes.map(size => sharp(Buffer.from(svg(tile(themes.dark)))).resize(size, size).png().toBuffer()));
const ico = Buffer.alloc(6 + 16 * sizes.length);
ico.writeUInt16LE(1, 2); ico.writeUInt16LE(sizes.length, 4);
let offset = ico.length;
for (const [index, size] of sizes.entries()) {
  const start = 6 + index * 16;
  ico.writeUInt8(size === 256 ? 0 : size, start); ico.writeUInt8(size === 256 ? 0 : size, start + 1);
  ico.writeUInt16LE(1, start + 4); ico.writeUInt16LE(32, start + 6);
  ico.writeUInt32LE(frames[index].length, start + 8); ico.writeUInt32LE(offset, start + 12);
  offset += frames[index].length;
}
await writeFile(resolve(output, 'sijing.ico'), Buffer.concat([ico, ...frames]));

const preview = `<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000">
  <rect width="1600" height="1000" fill="#f3f3f5"/>
  <g font-family="Segoe UI,Microsoft YaHei UI,Microsoft YaHei,sans-serif">
    <text x="104" y="93" fill="#777780" font-size="19" letter-spacing="3">SIJING / BRAND DIRECTION 01</text>
    <text x="104" y="203" fill="#222226" font-size="76" font-weight="600">思径</text>
    <text x="284" y="201" fill="#777780" font-size="42" font-weight="400">Sijing</text>
    <text x="104" y="260" fill="#66666e" font-size="26">看懂业务，找到依据。</text>
    <rect x="104" y="326" width="680" height="528" rx="32" fill="#e9e9ed"/>
    <rect x="816" y="326" width="680" height="528" rx="32" fill="#202023"/>
    <svg x="273" y="377" width="342" height="342" viewBox="0 0 1024 1024">${tile(themes.dark)}</svg>
    <svg x="985" y="377" width="342" height="342" viewBox="0 0 1024 1024">${tile(themes.light)}</svg>
    <text x="444" y="774" text-anchor="middle" fill="#333338" font-size="24" font-weight="500">石墨</text>
    <text x="1156" y="774" text-anchor="middle" fill="#f0f0f3" font-size="24" font-weight="500">银白</text>
    <text x="444" y="811" text-anchor="middle" fill="#74747c" font-size="16">一条思路，有迹可循</text>
    <text x="1156" y="811" text-anchor="middle" fill="#a3a3ad" font-size="16">同一轮廓，适配浅深界面</text>
    <text x="104" y="930" fill="#72727b" font-size="18">代码 · 业务地图 · 问答 · 只读数据</text>
    <g transform="translate(1220 892)">
      <svg x="0" y="18" width="24" height="24" viewBox="0 0 1024 1024">${tile(themes.dark)}</svg>
      <svg x="52" y="10" width="32" height="32" viewBox="0 0 1024 1024">${tile(themes.dark)}</svg>
      <svg x="112" y="-6" width="48" height="48" viewBox="0 0 1024 1024">${tile(themes.dark)}</svg>
      <svg x="188" y="-22" width="64" height="64" viewBox="0 0 1024 1024">${tile(themes.dark)}</svg>
    </g>
  </g>
</svg>`;
await writeFile(resolve(output, 'preview.svg'), preview);
await sharp(Buffer.from(preview)).png().toFile(resolve(output, 'preview.png'));
await writeFile(resolve(output, 'brand.json'), JSON.stringify({
  name: '思径', romanization: 'Sijing', pronunciation: 'sī jìng',
  tagline: '看懂业务，找到依据。', status: 'Approved product brand; applied in Sijing 0.2.8',
  concept: 'A rounded S-shaped route and an aligned endpoint. Simple recognition first, traceable understanding on closer inspection.',
  rationale: ['熟悉的两个字与简单轮廓降低识别负担。', '路径连接问题、业务地图与可核验来源。', '借鉴双加工理论，不宣称慢思考或美观能够保证答案正确。'],
  namingCheck: '2026-10-08 初步网页检索未发现明显同名同类软件；未完成商标、域名或包名核验。',
  sources: ['https://www.penguinrandomhouse.com/books/89308/thinking-fast-and-slow-by-daniel-kahneman/', 'https://academic.macmillan.com/academictrade/9780374533557/thinkingfastandslow/'],
}, null, 2));
console.log(`Sijing brand assets generated in ${output}`);
