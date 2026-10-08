/**
 * Mock 双语演示的**英文版素材**：中文素材在 `mockPapers.ts`，这里按 arXiv 编号给出对应的英文版。
 *
 * 为什么要分成两套：契约 §1「双语版本」里同一集的 `versions.zh` 与 `versions.en`
 * 各有独立的脚本与解读 —— 演示时点一下语言切换，看到的内容必须是**真的两种语言**，
 * 而不是同一份文本换个 key。所以英文版是单独写的一份，不是运行时翻译。
 *
 * 配图（封面 / 论文原图 / 信息图）**不在**这里：契约规定两版共用同一份配图。
 */
import type { MockAnalysis, MockScriptSegment, MockPaper, MockPaperMeta } from './mockPapers'
import {
  EN_ANALYSIS as ATTENTION_ANALYSIS,
  EN_META as ATTENTION_META,
  EN_SCRIPT as ATTENTION_SCRIPT,
} from './mockPapersEn/attention'
import {
  EN_ANALYSIS as RESNET_ANALYSIS,
  EN_META as RESNET_META,
  EN_SCRIPT as RESNET_SCRIPT,
} from './mockPapersEn/resnet'
import {
  EN_ANALYSIS as LORA_ANALYSIS,
  EN_META as LORA_META,
  EN_SCRIPT as LORA_SCRIPT,
} from './mockPapersEn/lora'
import {
  EN_ANALYSIS as GENERIC_ANALYSIS,
  EN_META as GENERIC_META,
  EN_SCRIPT as GENERIC_SCRIPT,
} from './mockPapersEn/generic'

/**
 * 英文版论文元信息里**分语言**的那部分（摘要 / 关键词）。
 *
 * 契约 §1 说两版的 `paper_meta` 通常一致，但那是针对「标题作者本来就都是英文」这种情况；
 * Mock 的中文版摘要与关键词是中文的，英文版沿用它就会出现「English 版本里摆着中文摘要」
 * 这种半成品观感，所以英文版单独提供一份。
 */
export interface MockPaperEn {
  analysis: MockAnalysis
  script: MockScriptSegment[]
  /** 该语言自己的摘要与关键词；标题 / 作者 / 年份 / 会议 / arXiv 编号两版共用 */
  meta: Pick<MockPaperMeta, 'abstract' | 'keywords'>
}

/** 兜底模板对应的论文（`MOCK_PAPERS` 里的第 4 篇，用户上传任意论文时套用它） */
const GENERIC_ARXIV_ID = '2305.01234'

const BY_ARXIV_ID: Record<string, MockPaperEn> = {
  // Attention Is All You Need
  '1706.03762': { analysis: ATTENTION_ANALYSIS, script: ATTENTION_SCRIPT, meta: ATTENTION_META },
  // Deep Residual Learning（ResNet）
  '1512.03385': { analysis: RESNET_ANALYSIS, script: RESNET_SCRIPT, meta: RESNET_META },
  // LoRA: Low-Rank Adaptation
  '2106.09685': { analysis: LORA_ANALYSIS, script: LORA_SCRIPT, meta: LORA_META },
  // 通用兜底模板
  [GENERIC_ARXIV_ID]: { analysis: GENERIC_ANALYSIS, script: GENERIC_SCRIPT, meta: GENERIC_META },
}

const GENERIC_EN: MockPaperEn = BY_ARXIV_ID[GENERIC_ARXIV_ID] ?? {
  analysis: GENERIC_ANALYSIS,
  script: GENERIC_SCRIPT,
  meta: GENERIC_META,
}

/** 取某篇论文的英文版素材；认不出来的论文一律退回通用模板（与中文侧的兜底口径一致） */
export function englishContentFor(paper: MockPaper): MockPaperEn {
  return BY_ARXIV_ID[paper.meta.arxiv_id] ?? GENERIC_EN
}
