/** 公众号文章主题（内联样式，颜色按账号） */
export const THEMES = {
  orange: {
    name: '橘色',
    heading: '#E67E22',
    accent: '#E67E22',
    accentDark: '#B85C0F',
    bg: '#FDF3E7',
    border: '#F0D9BD',
  },
  blue: {
    name: '经典蓝',
    heading: '#0F4C81',
    accent: '#0F4C81',
    accentDark: '#0A3559',
    bg: '#EDF2F8',
    border: '#C9D8EA',
  },
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function inline(s, t) {
  let x = escapeHtml(s);
  x = x.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1" style="max-width:100%;border-radius:6px;display:block;margin:10px 0;" />');
  x = x.replace(/\*\*([^*]+)\*\*/g, `<strong style="color:${t.accent};">$1</strong>`);
  x = x.replace(/`([^`]+)`/g, '<code style="background:#f0f0f0;border-radius:4px;padding:1px 6px;font-size:0.9em;">$1</code>');
  // LaTeX：微信不支持渲染，退化为等宽原样文本（去 $ 定界符）
  x = x.replace(/\$\$([^$]+?)\$\$/g, '<span style="font-family:Menlo,Consolas,monospace;background:#f6f7f8;padding:1px 6px;border-radius:4px;font-size:0.9em;">$1</span>');
  x = x.replace(/\$([^$\n]+?)\$/g, '<span style="font-family:Menlo,Consolas,monospace;background:#f6f7f8;padding:1px 4px;border-radius:4px;font-size:0.9em;">$1</span>');
  return x;
}

function splitRow(line) {
  let t = line.trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|')) t = t.slice(0, -1);
  return t.split('|').map((c) => c.trim());
}
function isTableSep(line) {
  const cells = splitRow(line);
  return cells.length > 0 && cells.every((c) => /^:?-{2,}:?$/.test(c));
}

/** 服务端 Markdown → 带主题内联样式的 HTML（供公众号文章正文）。 */
export function markdownToHtml(md, theme = 'orange') {
  const t = THEMES[theme] || THEMES.orange;
  const lines = String(md || '').replace(/\r\n?/g, '\n').split('\n');
  let html = '';
  let listType = null;
  let para = [];
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  const flushPara = () => {
    if (para.length) { html += `<p style="margin:0 0 0.8em;line-height:1.8;">${inline(para.join(' '), t)}</p>`; para = []; }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i].trimEnd().trim();
    if (!line) { flushPara(); closeList(); i++; continue; }

    if (line.startsWith('```')) {
      flushPara(); closeList();
      const lang = line.replace(/^```/, '').trim().toLowerCase();
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) { code.push(lines[i]); i++; }
      i++;
      if (lang === 'svg') {
        html += '<p style="color:#888;font-size:13px;text-align:center;">（图解为 SVG，微信正文不支持内嵌，请在原文 .md 或网页预览中查看）</p>';
      } else {
        html += `<pre style="background:#f6f7f8;border:1px solid #e5e7eb;border-radius:6px;padding:12px 14px;overflow-x:auto;font-size:13px;line-height:1.6;"><code>${escapeHtml(code.join('\n'))}</code></pre>`;
      }
      continue;
    }

    if (line.startsWith('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flushPara(); closeList();
      const header = splitRow(line);
      const body = [];
      let j = i + 2;
      while (j < lines.length && lines[j].trim().startsWith('|')) { body.push(splitRow(lines[j])); j++; }
      let tbl = '<table style="border-collapse:collapse;width:100%;margin:12px 0;font-size:14px;">';
      tbl += '<thead><tr>';
      header.forEach((h) => {
        tbl += `<th style="background:${t.bg};color:${t.accentDark};border:1px solid #e0e0e0;padding:6px 10px;text-align:left;font-weight:700;">${inline(h, t)}</th>`;
      });
      tbl += '</tr></thead><tbody>';
      body.forEach((row) => {
        tbl += '<tr>';
        row.forEach((c) => { tbl += `<td style="border:1px solid #e0e0e0;padding:6px 10px;">${inline(c, t)}</td>`; });
        tbl += '</tr>';
      });
      tbl += '</tbody></table>';
      html += tbl;
      i = j;
      continue;
    }

    const h = line.match(/^(#{1,4})\s+(.+)$/);
    if (h) {
      flushPara(); closeList();
      const level = Math.min(h[1].length + 1, 4);
      html += `<h${level} style="color:${t.heading};border-bottom:2px solid ${t.border};padding-bottom:6px;margin:1.2em 0 0.6em;">${inline(h[2], t)}</h${level}>`;
      i++; continue;
    }
    const ul = line.match(/^[-*]\s+(.+)$/);
    if (ul) {
      flushPara();
      if (listType !== 'ul') { closeList(); html += '<ul style="margin:0 0 0.8em;padding-left:1.4em;">'; listType = 'ul'; }
      html += `<li style="margin:4px 0;">${inline(ul[1], t)}</li>`;
      i++; continue;
    }
    const ol = line.match(/^\d+[.)]\s+(.+)$/);
    if (ol) {
      flushPara();
      if (listType !== 'ol') { closeList(); html += '<ol style="margin:0 0 0.8em;padding-left:1.4em;">'; listType = 'ol'; }
      html += `<li style="margin:4px 0;">${inline(ol[1], t)}</li>`;
      i++; continue;
    }
    const bq = line.match(/^>\s?(.+)$/);
    if (bq) {
      flushPara(); closeList();
      html += `<blockquote style="border-left:4px solid ${t.accent};background:${t.bg};color:#555;padding:10px 14px;border-radius:0 6px 6px 0;margin:1em 0;">${inline(bq[1], t)}</blockquote>`;
      i++; continue;
    }
    para.push(line);
    i++;
  }
  flushPara();
  closeList();
  return html;
}

/** Markdown → 纯文本（用于公众号摘要 digest 等）。 */
export function plainFromMarkdown(md) {
  return String(md || '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((l) =>
      l
        .replace(/^\s*#{1,4}\s+/, '')
        .replace(/^\s*>\s?/, '')
        .replace(/^\s*[-*]\s+/, '• ')
        .replace(/^\s*\d+[.)]\s+/, ''),
    )
    .join('\n')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\$\$([^$]+?)\$\$/g, '$1')
    .replace(/\$([^$\n]+?)\$/g, '$1')
    .trim();
}
