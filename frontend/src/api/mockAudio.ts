/**
 * Mock 模式的音频合成：用 Web Audio API（OfflineAudioContext）现场渲染一段正弦波音频，
 * 再编码成 16bit PCM WAV Blob —— 这样 <audio> 可以直接播放，无需后端、无需密钥。
 *
 * 为了让演示听起来像「两个主播在说话」，每个脚本段落用一个基频，
 * 并叠加 ~4.5Hz 的振幅调制模拟音节节奏（不是音乐，是节奏化的正弦波）。
 */

type OfflineAudioContextCtor = new (
  numberOfChannels: number,
  length: number,
  sampleRate: number,
) => OfflineAudioContext

export interface RenderMockAudioParams {
  durationSec: number
  /** 每个段落的说话人，用来切换音色（A 低沉 / B 明亮） */
  speakers: Array<'A' | 'B'>
  sampleRate?: number
}

export interface RenderMockAudioResult {
  blob: Blob
  durationSec: number
  sampleRate: number
}

const SAMPLE_RATE = 24000

/** 主播A / 主播B 的基频（Hz）与谐波配比 */
const VOICE_TONE = {
  A: { base: 145, fifth: 218, gain: 0.34 },
  B: { base: 232, fifth: 348, gain: 0.3 },
} as const

export async function renderMockAudio(params: RenderMockAudioParams): Promise<RenderMockAudioResult> {
  const sampleRate = params.sampleRate ?? SAMPLE_RATE
  const durationSec = Math.max(4, Math.round(params.durationSec))
  const frameCount = Math.ceil(durationSec * sampleRate)

  const buffer = await renderWithWebAudio(params.speakers, frameCount, sampleRate, durationSec)
  const blob = encodeWav(buffer, sampleRate)
  return { blob, durationSec, sampleRate }
}

/** 用 OfflineAudioContext 离线渲染（不需要用户手势，也不会真的出声） */
async function renderWithWebAudio(
  speakers: Array<'A' | 'B'>,
  frameCount: number,
  sampleRate: number,
  durationSec: number,
): Promise<Float32Array> {
  const scope = globalThis as unknown as {
    OfflineAudioContext?: OfflineAudioContextCtor
    webkitOfflineAudioContext?: OfflineAudioContextCtor
  }
  const Ctor = scope.OfflineAudioContext ?? scope.webkitOfflineAudioContext
  if (typeof Ctor !== 'function') return synthesizeByHand(speakers, frameCount, sampleRate, durationSec)

  const ctx = new Ctor(1, frameCount, sampleRate)
  const segments: Array<'A' | 'B'> = speakers.length ? speakers : ['A', 'B']
  const segLen = durationSec / segments.length

  const master = ctx.createGain()
  master.gain.value = 0.9
  master.connect(ctx.destination)

  segments.forEach((speaker, index) => {
    const tone = VOICE_TONE[speaker] ?? VOICE_TONE.A
    const start = index * segLen
    const end = Math.min(durationSec, start + segLen)
    if (end - start < 0.05) return

    const gain = ctx.createGain()
    const envelope = tone.gain
    gain.gain.setValueAtTime(0.0001, start)
    gain.gain.linearRampToValueAtTime(envelope, start + 0.04)
    gain.gain.setValueAtTime(envelope, Math.max(start + 0.05, end - 0.06))
    gain.gain.linearRampToValueAtTime(0.0001, end)
    gain.connect(master)

    // 音节节奏：4.5Hz 正弦调制振幅，听起来像在断句
    const lfo = ctx.createOscillator()
    lfo.type = 'sine'
    lfo.frequency.value = 4.5 + ((index * 7) % 5) * 0.1
    const lfoDepth = ctx.createGain()
    lfoDepth.gain.value = envelope * 0.45
    lfo.connect(lfoDepth)
    lfoDepth.connect(gain.gain)
    lfo.start(start)
    lfo.stop(end)

    // 基频 + 五度泛音，稍微有点「人声」的厚度
    const carriers: Array<[number, number]> = [
      [tone.base, 1],
      [tone.fifth, 0.32],
    ]
    carriers.forEach(([freq, level]) => {
      const osc = ctx.createOscillator()
      osc.type = 'sine'
      osc.frequency.value = freq
      // 轻微的音高起伏，避免机械感
      const drift = ctx.createOscillator()
      drift.type = 'sine'
      drift.frequency.value = 0.7
      const driftDepth = ctx.createGain()
      driftDepth.gain.value = freq * 0.012
      drift.connect(driftDepth)
      driftDepth.connect(osc.frequency)
      drift.start(start)
      drift.stop(end)

      const levelGain = ctx.createGain()
      levelGain.gain.value = level
      osc.connect(levelGain)
      levelGain.connect(gain)
      osc.start(start)
      osc.stop(end)
    })
  })

  const rendered = await ctx.startRendering()
  return rendered.getChannelData(0)
}

/** 兜底：Web Audio 不可用时直接算正弦波，保证播放器依然有源可播 */
function synthesizeByHand(
  speakers: Array<'A' | 'B'>,
  frameCount: number,
  sampleRate: number,
  durationSec: number,
): Float32Array {
  const out = new Float32Array(frameCount)
  const segments: Array<'A' | 'B'> = speakers.length ? speakers : ['A', 'B']
  const segLen = durationSec / segments.length

  for (let i = 0; i < frameCount; i += 1) {
    const t = i / sampleRate
    const index = Math.min(segments.length - 1, Math.floor(t / segLen))
    const tone = VOICE_TONE[segments[index] ?? 'A'] ?? VOICE_TONE.A
    const syllable = 0.6 + 0.4 * Math.sin(2 * Math.PI * 4.5 * t)
    const wave =
      Math.sin(2 * Math.PI * tone.base * t) + 0.32 * Math.sin(2 * Math.PI * tone.fifth * t)
    out[i] = Math.max(-1, Math.min(1, wave * tone.gain * syllable * 0.9))
  }
  return out
}

/** Float32 单声道 → 16bit PCM WAV */
export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const bytesPerSample = 2
  const dataBytes = samples.length * bytesPerSample
  const buffer = new ArrayBuffer(44 + dataBytes)
  const view = new DataView(buffer)

  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true) // PCM 子块大小
  view.setUint16(20, 1, true) // 格式：PCM
  view.setUint16(22, 1, true) // 声道数：单声道
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * bytesPerSample, true) // 字节率
  view.setUint16(32, bytesPerSample, true) // 块对齐
  view.setUint16(34, 16, true) // 位深
  writeAscii(view, 36, 'data')
  view.setUint32(40, dataBytes, true)

  let offset = 44
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i] ?? 0))
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
    offset += bytesPerSample
  }

  return new Blob([buffer], { type: 'audio/wav' })
}

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i))
}
