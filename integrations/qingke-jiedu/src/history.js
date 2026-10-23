import fs from 'node:fs/promises';
import path from 'node:path';
import { config } from './config.js';

const MAX = 50; // 最多保留条数
const file = path.join(config.outputDir, 'history.json');

/** 读取历史记录（最近在前）。 */
export async function loadHistory() {
  try {
    const raw = await fs.readFile(file, 'utf-8');
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

/** 追加一条历史（按 url 去重、置顶、封顶 MAX）。 */
export async function addHistory(entry) {
  const list = await loadHistory();
  const next = [
    {
      url: entry.url || '',
      title: entry.title || '',
      type: entry.type || '',
      at: entry.at || new Date().toISOString(),
    },
    ...list.filter((e) => e && e.url !== entry.url),
  ].slice(0, MAX);

  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, JSON.stringify(next, null, 2), 'utf-8');
  return next;
}

/** 清空历史。 */
export async function clearHistory() {
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, '[]', 'utf-8');
}
