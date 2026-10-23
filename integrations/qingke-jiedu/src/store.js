import fs from 'node:fs/promises';
import path from 'node:path';
import AdmZip from 'adm-zip';
import { config } from './config.js';

/** 单次结果的目录：output/{id}/ */
export function resultDir(id) {
  return path.join(config.outputDir, id);
}

function buildMarkdown(meta) {
  const lines = [];
  lines.push(`# ${meta.title || meta.sourceTitle || '未命名内容'}`);
  lines.push('');
  lines.push(`> 来源：${meta.url}`);
  lines.push(`> 类型：${meta.type === 'pdf' ? '论文 PDF' : meta.type === 'text' ? '文章' : '网页'}`);
  if (meta.sourceTitle) lines.push(`> 原文标题：${meta.sourceTitle}`);
  if (meta.institution) lines.push(`> 机构：${meta.institution}`);
  if (meta.date) lines.push(`> 时间：${meta.date}`);
  lines.push('');
  lines.push('## 解读文案');
  lines.push('');
  lines.push(
    meta.copy ||
      '（解读文案尚未生成：图片已就绪，可先下载；模型可用后在页面点「重新生成文案」补上）',
  );
  lines.push('');
  lines.push('## 备选标题');
  lines.push('');
  for (const t of meta.titles || []) lines.push(`- ${t}`);
  lines.push('');
  lines.push('## 图片清单');
  lines.push('');
  for (const img of meta.images || []) {
    lines.push(`- ${img.filename} — ${img.label}（${img.width}×${img.height}）`);
  }
  lines.push('');
  return lines.join('\n');
}

/** 读取 result.json（文件不存在时抛 ENOENT，由路由层转成 404）。 */
export async function readResult(id) {
  return JSON.parse(await fs.readFile(path.join(resultDir(id), 'result.json'), 'utf-8'));
}

/** 写入图片文件，返回图片清单（供 result.json 与前端使用）。 */
export async function saveImages(id, images) {
  const dir = resultDir(id);
  await fs.mkdir(dir, { recursive: true });

  const saved = [];
  for (const img of images) {
    const filename = `${String(img.pageNumber).padStart(3, '0')}.png`;
    await fs.writeFile(path.join(dir, filename), img.buffer);
    saved.push({
      filename,
      pageNumber: img.pageNumber,
      width: img.width,
      height: img.height,
      label: img.label,
    });
  }
  return saved;
}

/** 按记录整份写入 result.json。 */
export async function writeResult(id, record) {
  await fs.mkdir(resultDir(id), { recursive: true });
  await fs.writeFile(
    path.join(resultDir(id), 'result.json'),
    JSON.stringify(record),
    'utf-8',
  );
}

/** 按 result.json 的记录（重）写 summary.md 与 images.zip。 */
async function writeBundle(id, record) {
  const dir = resultDir(id);
  const md = buildMarkdown(record);
  await fs.writeFile(path.join(dir, 'summary.md'), md, 'utf-8');

  const zip = new AdmZip();
  for (const img of record.images || []) {
    zip.addFile(img.filename, await fs.readFile(path.join(dir, img.filename)));
  }
  zip.addFile('summary.md', Buffer.from(md, 'utf-8'));
  zip.writeZip(path.join(dir, 'images.zip'));

  return md;
}

/**
 * 阶段一：图片落盘 + result.json（文案待生成）+ 打包。
 * 不涉及任何模型调用，模型不可用时这一步照样成功。
 */
export async function savePrepared(id, images, meta) {
  const saved = await saveImages(id, images);
  const record = {
    type: meta.type,
    url: meta.url || '',
    sourceTitle: meta.sourceTitle || '',
    institution: meta.institution || '',
    date: meta.date || '',
    source: meta.source || null, // 供阶段二生成文案时复用（正文、术语、机构…）
    images: saved,
    // 文案相关字段：阶段二再填
    title: '',
    titles: [],
    copy: '',
    reasoning: '',
    style: null,
    copyStatus: 'pending',
    copyError: '',
  };
  await writeResult(id, record);
  await writeBundle(id, record);
  return { images: saved, record };
}

/** 阶段二：把生成的文案写回 result.json，并重打包（summary.md / images.zip）。 */
export async function saveCopy(id, patch) {
  const record = {
    ...(await readResult(id)),
    ...patch,
    copyStatus: 'done',
    copyError: '',
  };
  await writeResult(id, record);
  await writeBundle(id, record);
  return record;
}

/** 文案生成失败：只记录状态，图片与打包结果保持可用。 */
export async function markCopyError(id, message) {
  try {
    const record = await readResult(id);
    await writeResult(id, {
      ...record,
      copyStatus: 'error',
      copyError: String(message || ''),
    });
  } catch {
    /* 记录失败不影响已经落盘的图片 */
  }
}
