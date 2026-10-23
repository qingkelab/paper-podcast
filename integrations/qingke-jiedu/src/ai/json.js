/**
 * 从模型输出里宽松地抠出 JSON（本地小模型常带前后废话、码块围栏或多余逗号）。
 */
export function parseJsonLoose(content) {
  const text = String(content || '').trim();
  if (!text) return null;

  const candidates = [text];
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) candidates.push(fenced[1].trim());
  const first = text.indexOf('{');
  const last = text.lastIndexOf('}');
  if (first >= 0 && last > first) candidates.push(text.slice(first, last + 1));
  const arrFirst = text.indexOf('[');
  const arrLast = text.lastIndexOf(']');
  if (arrFirst >= 0 && arrLast > arrFirst) candidates.push(text.slice(arrFirst, arrLast + 1));

  for (const c of candidates) {
    for (const variant of [c, stripTrailingCommas(c)]) {
      try {
        return JSON.parse(variant);
      } catch {
        /* 继续尝试下一个候选 */
      }
    }
  }
  return null;
}

function stripTrailingCommas(s) {
  return String(s)
    .replace(/,\s*([}\]])/g, '$1')
    // 清理 JSON 解析残留的控制字符（有意使用控制字符集）
    // oxlint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f]+/g, ' ');
}
