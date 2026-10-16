import sharp from 'sharp';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const output = resolve(dirname(fileURLToPath(import.meta.url)), '../build-resources');
await mkdir(output, { recursive: true });
const svg = await readFile(resolve(output, 'branding/sijing/icon-dark.svg'), 'utf8');
await writeFile(resolve(output, 'icon.svg'), svg);
await sharp(Buffer.from(svg)).resize(1024, 1024).png().toFile(resolve(output, 'icon.png'));
const sizes = [16, 24, 32, 48, 64, 128, 256];
const frames = await Promise.all(sizes.map(size => sharp(Buffer.from(svg)).resize(size, size).png().toBuffer()));
const header = Buffer.alloc(6 + 16 * sizes.length);
header.writeUInt16LE(1, 2); header.writeUInt16LE(sizes.length, 4);
let offset = header.length;
for (const [index, size] of sizes.entries()) {
  const start = 6 + index * 16;
  header.writeUInt8(size === 256 ? 0 : size, start); header.writeUInt8(size === 256 ? 0 : size, start + 1);
  header.writeUInt16LE(1, start + 4); header.writeUInt16LE(32, start + 6);
  header.writeUInt32LE(frames[index].length, start + 8); header.writeUInt32LE(offset, start + 12);
  offset += frames[index].length;
}
await writeFile(resolve(output, 'icon.ico'), Buffer.concat([header, ...frames]));
console.log('Sijing desktop icons generated.');
