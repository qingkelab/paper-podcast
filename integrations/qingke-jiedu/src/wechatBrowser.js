import { spawn, execFile } from 'node:child_process';
import path from 'node:path';
import { config } from './config.js';

const profileDir = path.join(config.root, '.wechat-chrome-profile');

/** 复制文本到 macOS 剪贴板（best-effort，失败返回 false）。 */
export function copyToClipboard(text) {
  return new Promise((resolve) => {
    try {
      const p = execFile('pbcopy', [], (err) => resolve(!err));
      p.stdin.on('error', () => resolve(false));
      p.stdin.end(String(text || ''));
    } catch {
      resolve(false);
    }
  });
}

/**
 * 用系统 Chrome 打开公众号后台（独立持久会话，登录一次即可）。
 * 浏览器方式不依赖 IP 白名单——API 方式因白名单失效时用它兜底手动发布。
 */
export function launchWechatBrowser() {
  const args = [
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--window-size=1280,900',
    'https://mp.weixin.qq.com/',
  ];
  const child = spawn(config.chromePath, args, { detached: true, stdio: 'ignore' });
  child.on('error', () => {}); // 启动失败静默，交给上层判断
  child.unref();
  return true;
}
