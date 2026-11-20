import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

// 保存隨瀏覽器套件散佈的第三方授權，略過只在編譯時使用的套件。
const { packages } = JSON.parse(readFileSync('package-lock.json', 'utf8'));
const notices = new Map();
for (const [path, metadata] of Object.entries(packages)) {
  if (!path || metadata.dev || !existsSync(path)) continue;
  const name = path.split('node_modules/').at(-1);
  const title = name + '@' + metadata.version;
  if (notices.has(title)) continue;
  const files = readdirSync(path).filter(file => /^(license|copying)(\.|$)/i.test(file)).sort();
  if (files.length) {
    notices.set(title, title + '\n\n' + files.map(file => readFileSync(join(path, file), 'utf8').trim()).join('\n\n'));
  }
}
writeFileSync('../app/static/editor-licenses.txt', [...notices].sort().map(([, notice]) => notice).join('\n\n' + '='.repeat(72) + '\n\n') + '\n');
