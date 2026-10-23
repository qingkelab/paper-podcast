import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { writeFile } from 'node:fs/promises';
import { config } from '../config.js';

const execFileP = promisify(execFile);

/** MiniMax（speech-02-hd 高保真）与 Edge（免费）的可用音色（前端选择用）。 */
export const MINIMAX_VOICES = {
  'male-qn-qingse': '青涩少年',
  'male-qn-jingying': '精英男声',
  'male-qn-daxuesheng': '阳光学长',
  'male-qn-badao': '低沉磁性',
  'female-shaonv': '元气少女',
  'female-chengshu': '成熟女声',
  'female-tianmei': '甜美可爱',
  'female-yujie': '御姐气场',
  presentation_male: '讲解男声',
  presentation_female: '讲解女声',
};
export const EDGE_VOICES = {
  'zh-CN-YunxiNeural': '云希（男·自然）',
  'zh-CN-XiaoyiNeural': '晓伊（女·温柔）',
  'zh-CN-YunjianNeural': '云健（男·磁性）',
  'zh-CN-XiaoxiaoNeural': '晓晓（女·甜美）',
  'zh-CN-YunyangNeural': '云扬（男·新闻）',
};

/** 解析实际配音引擎：auto → 有 MiniMax key 用 minimax，否则 edge。 */
export function resolveEngine() {
  const e = config.ttsEngine;
  if (e === 'minimax') return 'minimax';
  if (e === 'edge') return 'edge';
  return config.minimaxApiKey ? 'minimax' : 'edge';
}

/** edge 风格 "+10%" → 1.1（MiniMax speed 用倍率）；纯数字按倍率。 */
export function parseRate(rate) {
  const s = String(rate || '').trim();
  if (!s) return 1.0;
  const m = s.match(/^([+-])(\d+(?:\.\d+)?)%$/);
  if (m) return 1.0 + (m[1] === '+' ? 1 : -1) * Number(m[2]) / 100;
  const n = Number(s);
  return Number.isFinite(n) ? n : 1.0;
}

async function ffprobeDuration(file) {
  try {
    const { stdout } = await execFileP(
      'ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file],
      { timeout: 60000 },
    );
    const d = Number(String(stdout).trim());
    return Number.isFinite(d) ? d : 0;
  } catch {
    return 0;
  }
}

async function synthMinimax(text, voice, rate, outPath) {
  if (!config.minimaxApiKey) throw new Error('MiniMax TTS 未配置 MINIMAX_API_KEY');
  const body = {
    model: config.minimaxTtsModel,
    text,
    voice_setting: { voice_id: voice, speed: Math.max(0.5, Math.min(2.0, parseRate(rate))), vol: 1.0, pitch: 0 },
    audio_setting: { sample_rate: 32000, bitrate: 128000, format: 'mp3', channel: 1 },
  };
  const res = await fetch(`${config.minimaxBaseUrl}/t2a_v2`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.minimaxApiKey}` },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(120000),
  });
  if (!res.ok) throw new Error(`MiniMax TTS HTTP ${res.status}`);
  const data = await res.json();
  const base = data?.base_resp || {};
  if (base.status_code && base.status_code !== 0) {
    throw new Error(`MiniMax TTS 错误：${base.status_msg || base.status_code}`);
  }
  const audio = data?.data?.audio;
  if (!audio) throw new Error('MiniMax TTS 响应缺少 audio 字段');
  const raw = Buffer.from(String(audio), 'hex');
  await writeFile(outPath, raw);
}

async function synthEdge(text, voice, rate, outPath) {
  // 优先走 edge-tts CLI（本机已安装）；失败则尝试 python3 -m edge_tts
  const args = ['-t', text, '-v', voice, '--rate', rate || '+0%', '--write-media', outPath];
  try {
    await execFileP('edge-tts', args, { timeout: 120000, maxBuffer: 10 * 1024 * 1024 });
  } catch {
    await execFileP('python3', ['-m', 'edge_tts', ...args], { timeout: 120000, maxBuffer: 10 * 1024 * 1024 });
  }
}

export async function synthOne(text, engine, voice, rate, outPath) {
  if (engine === 'minimax') await synthMinimax(text, voice, rate, outPath);
  else await synthEdge(text, voice, rate, outPath);
  const dur = await ffprobeDuration(outPath);
  if (dur <= 0) throw new Error('语音合成结果时长解析异常');
  return dur;
}

/**
 * 逐场景合成旁白 mp3（每场景一次），把 audio/duration 写回 scene。
 * 单个场景失败重试 3 次；最终仍失败抛错（带场景号）。
 */
export async function synthesizeScenes(scenes, workDir, { engine, voice, rate } = {}, onProgress) {
  const eng = engine || resolveEngine();
  if (eng === 'minimax' && !config.minimaxApiKey) throw new Error('TTS 引擎为 MiniMax，但未配置 MINIMAX_API_KEY');
  const v = voice || (eng === 'minimax' ? 'male-qn-qingse' : 'zh-CN-YunxiNeural');
  const n = scenes.length;
  const out = [];
  for (let i = 0; i < n; i++) {
    const audioPath = `${workDir}/audio_${String(i).padStart(2, '0')}.mp3`;
    let dur = 0;
    let lastErr = null;
    for (let attempt = 0; attempt < 3 && !dur; attempt++) {
      try {
        dur = await synthOne(scenes[i].narration, eng, v, rate || config.ttsRate, audioPath);
      } catch (err) {
        lastErr = err;
      }
    }
    if (!dur) {
      throw new Error(`语音合成失败（${eng}，场景 ${i + 1}）：${(lastErr && lastErr.message) || lastErr}`);
    }
    scenes[i].audio = audioPath;
    scenes[i].duration = Math.round(dur * 1000) / 1000;
    out.push(scenes[i]);
    if (onProgress) onProgress(i + 1, n);
  }
  return { engine: eng, voice: v, rate: rate || config.ttsRate, scenes };
}
