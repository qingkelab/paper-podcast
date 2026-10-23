import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { config } from '../config.js';

const execFileP = promisify(execFile);

export async function ffprobeDuration(file) {
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

/**
 * 合成 mp4：每个场景一张静态画面 + 一段旁白音频，
 * 视频用 xfade（fade）、音频用 acrossfade 交叉淡化串起来（无运镜/无字幕动画）。
 * 返回 { path, duration }。
 */
export async function buildVideo(scenes, workDir, log = () => {}) {
  const n = scenes.length;
  if (!n) throw new Error('没有可合成的场景');
  const W = config.videoWidth;
  const H = config.videoHeight;
  const fps = config.videoFps;
  const xfade = config.xfadeSeconds;

  log(`准备 ffmpeg 合成：${n} 个场景…`);
  const cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error'];

  // 输入（每个场景：先图后音频），记录每个输入的下标
  const vidIdx = [];
  const audIdx = [];
  for (let i = 0; i < n; i++) {
    if (!scenes[i].visual || !scenes[i].audio) throw new Error(`场景 ${i + 1} 缺少画面或音频`);
    vidIdx.push(cmd.filter((c) => c === '-i').length);
    cmd.push('-i', scenes[i].visual);
    audIdx.push(cmd.filter((c) => c === '-i').length);
    cmd.push('-i', scenes[i].audio);
  }

  const segFrames = scenes.map((s) => Math.max(1, Math.round(s.duration * fps)));
  const segDurs = segFrames.map((f) => f / fps);

  const fc = [];
  for (let i = 0; i < n; i++) {
    fc.push(
      `[${vidIdx[i]}:v]scale=${W}:${H}:force_original_aspect_ratio=increase,` +
        `crop=${W}:${H},fps=${fps},format=yuv420p,` +
        `loop=loop=${segFrames[i] - 1}:size=1:start=0,setpts=PTS-STARTPTS[v${i}]`,
    );
    fc.push(
      `[${audIdx[i]}:a]aformat=sample_rates=44100:channel_layouts=stereo,` +
        `atrim=0:${segDurs[i].toFixed(3)},asetpts=PTS-STARTPTS[a${i}]`,
    );
  }

  // 视频 xfade 链
  let prev = 'v0';
  let total = segDurs[0];
  for (let i = 1; i < n; i++) {
    const offset = Math.max(0, total - xfade);
    const label = i < n - 1 ? `x${i}` : 'vout';
    fc.push(`[${prev}][v${i}]xfade=transition=fade:duration=${xfade}:offset=${offset.toFixed(3)}[${label}]`);
    prev = label;
    total = total + segDurs[i] - xfade;
  }
  // 音频 acrossfade 链
  let aprev = '[a0]';
  for (let i = 1; i < n; i++) {
    const label = i < n - 1 ? `[xa${i}]` : '[aout]';
    fc.push(`${aprev}[a${i}]acrossfade=d=${xfade}${label}`);
    aprev = label;
  }

  cmd.push('-filter_complex', fc.join(';'));
  const voutLabel = n === 1 ? 'v0' : 'vout';
  const aoutLabel = n === 1 ? 'a0' : 'aout';
  cmd.push('-map', `[${voutLabel}]`, '-map', `[${aoutLabel}]`);
  cmd.push('-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p');
  cmd.push('-c:a', 'aac', '-b:a', '192k', '-ar', '44100');
  cmd.push('-t', total.toFixed(3), '-movflags', '+faststart');

  const out = `${workDir}/podcast.mp4`;
  cmd.push(out);

  log(`ffmpeg 合成中（预计 ${Math.round(total)}s，${n} 个场景转场）…`);
  try {
    await execFileP(cmd[0], cmd.slice(1), { timeout: 2400000, maxBuffer: 50 * 1024 * 1024 });
  } catch (err) {
    const stderr = err && err.stderr ? String(err.stderr) : String(err);
    throw new Error(`视频合成失败：${stderr.slice(-3000)}`);
  }
  const dur = await ffprobeDuration(out);
  log(`视频合成完成：${dur.toFixed(1)}s`);
  return { path: out, duration: Math.round(dur * 10) / 10 };
}
