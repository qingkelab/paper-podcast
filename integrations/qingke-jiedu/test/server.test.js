// 冒烟测试：服务能启动、/api/health 与 /api/copy、/api/images 的校验分支
// 用独立端口与独立 output 目录，避免污染本机数据；不触发任何真实模型调用。
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';

const tmpOut = fs.mkdtempSync(path.join(os.tmpdir(), 'qkj-server-'));
const PORT = 4971;

import test from 'node:test';
import assert from 'node:assert/strict';

async function waitForHealth(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/api/health`);
      if (res.ok) return await res.json();
    } catch {
      /* 还没起来 */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error('服务未在预期时间内启动');
}

test('服务可启动，且 /api/copy 与 /api/images 的基础校验正常', async (t) => {
  const child = spawn(process.execPath, ['server.js'], {
    cwd: path.resolve(import.meta.dirname, '..'),
    env: {
      ...process.env,
      PORT: String(PORT),
      OUTPUT_DIR: tmpOut,
      QUALITY_REVIEW: '0',
      LLM_PROVIDER: 'ollama',
    },
    stdio: 'ignore',
  });
  t.after(() => child.kill('SIGTERM'));

  const health = await waitForHealth();
  assert.equal(health.ok, true);
  assert.ok('apiConfigured' in health, 'health 应带 apiConfigured');

  // 普通图文解读：缺 url 应 400（说明 /api/copy 相关路由未被改动破坏）
  const badImages = await fetch(`http://127.0.0.1:${PORT}/api/images`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  assert.equal(badImages.status, 400);

  const badCopy = await fetch(`http://127.0.0.1:${PORT}/api/copy`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  assert.equal(badCopy.status, 400, '缺 id 应返回 400');

  const missingCopy = await fetch(`http://127.0.0.1:${PORT}/api/copy`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: '00000000-0000-0000-0000-000000000000' }),
  });
  assert.equal(missingCopy.status, 404, '不存在的 id 应返回 404（不再调用模型）');

  const stats = await fetch(`http://127.0.0.1:${PORT}/api/providers`).then((r) => r.json());
  assert.ok(stats.api, 'providers 应返回默认 API provider');
});
