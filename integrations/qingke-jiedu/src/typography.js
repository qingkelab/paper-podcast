/** 中文排版规范化：CJK 与半角字母/数字间加空格、直引号转弯引号；不破坏 URL / 代码 / 图片。 */

function stashPush(arr, s) {
  const idx = arr.length;
  arr.push(s);
  return `\u0000${idx}\u0000`;
}

export function normalizeTypography(text) {
  let t = String(text || '').replace(/\r\n?/g, '\n');
  const stash = [];

  // 保护：围栏代码块、图片、URL、行内代码（避免排版改动破坏它们）
  t = t.replace(/```[\s\S]*?```/g, (m) => stashPush(stash, m));
  t = t.replace(/!\[[^\]]*\]\([^)]+\)/g, (m) => stashPush(stash, m));
  t = t.replace(/(https?:\/\/[^\s"'<>()]+)/g, (m) => stashPush(stash, m));
  t = t.replace(/`[^`\n]+`/g, (m) => stashPush(stash, m));

  // 1) 中文与半角字母/数字之间加一个空格（盘古之白）
  t = t.replace(/([\u3400-\u4dbf\u4e00-\u9fff])([A-Za-z0-9])/g, '$1 $2');
  t = t.replace(/([A-Za-z0-9])([\u3400-\u4dbf\u4e00-\u9fff])/g, '$1 $2');

  // 2) 成对的英文直引号 → 中文弯引号
  t = t.replace(/"([^"\n]{1,80})"/g, '“$1”');
  t = t.replace(/'([^'\n]{1,40})'/g, '‘$1’');

  // 还原受保护内容
  // 占位符用 \u0000 包裹（有意使用控制字符）
  // oxlint-disable-next-line no-control-regex
  t = t.replace(/\u0000(\d+)\u0000/g, (_m, i) => stash[Number(i)] ?? '');

  return t;
}
