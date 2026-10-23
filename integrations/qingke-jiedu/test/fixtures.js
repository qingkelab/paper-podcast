/**
 * 测试用论文素材：
 *  - longPaperText()：>30k 字的合成论文（章节结构 + 数字 + 公式 + 图注 + 消融 + 局限），
 *    关键实验数字只出现在「后半篇」的实验/消融章节，用来验证检索不再只看前 16000 字。
 *  - paperHtml()：arXiv HTML 风格的 DOM（ltx_section / figure / table / MathML）。
 *  - paperTex()：latexToText() 产物形态（## 章节 / 图注： / $$公式$$）。
 *  - paperPdfLines()：PDF 抽出的带换行正文（编号标题）。
 */

const FILLER =
  '这一段用于填充正文长度，讨论方法的动机、直觉与实现细节，并说明它与既有做法的差别与代价。' +
  '我们强调每个判断都要有依据，不能靠堆砌术语；同时保留工程上关心的开销、吞吐与可复现性讨论。';

function filler(n) {
  return Array.from({ length: n }, () => `${FILLER}`).join('');
}

/** 合成一篇 30k+ 字论文。 */
export function longPaperText() {
  const parts = [];
  parts.push(`## 摘要\n我们提出 LoopFormer，一个把循环深度与窗口注意力结合的架构。在 WMT 与 LongBench 上，BLEU 达到 41.8，长文问答准确率提升至 73.2%。训练只用了 8 块 A100、3.5 天。${filler(30)}`);
  parts.push(`## 1 Introduction\n长上下文建模的瓶颈在于注意力复杂度。既有做法把上下文截断到 4096 token，导致后半篇信息丢失。论文的问题是：如何在固定显存下让每个 token 都能看到全局状态。${filler(45)}`);
  parts.push(`## 2 Related Work\n早期工作依赖稀疏注意力与线性注意力；另一些工作用检索增强。我们的差别在于把循环状态写进 KV 缓存。${filler(40)}`);
  parts.push(`## 3 Method\n### 3.1 Recurrent State Update\n核心组件是状态更新算子：输入是上一时刻的状态 h_{t-1} 与当前 token 嵌入 e_t，经过门控后输出 h_t。最小例子：t=1 时状态为空，t=2 时门控给历史 0.82 的权重，t=3 时输出已经稳定。${filler(50)}`);
  parts.push(`$$h_t = \\alpha \\odot h_{t-1} + (1-\\alpha) \\odot W e_t$$\n\n其中 W 是投影矩阵、alpha 是门控系数、odot 是逐元素乘。该式的关键是把历史状态按比例带回当前计算。`);
  parts.push(`### 3.2 Windowed Attention\n窗口注意力只在最近 W 个 token 上做注意力，其余信息由循环状态承载。我们在实现上把两者合并成一次 kernel 调用。${filler(45)}`);
  parts.push(`图注：图 1 LoopFormer 整体架构：左侧是循环状态更新，右侧是窗口注意力，状态沿着时间轴滚动传递。`);
  parts.push(`## 4 Experiments\n### 4.1 Setup\n我们在 WMT14 与 LongBench 上评测，基线包括 Transformer-base 与 Performer，指标用 BLEU 与准确率。${filler(30)}`);
  parts.push(`### 4.2 Main Results\nLoopFormer 在 WMT14 英德上达到 BLEU 41.8，比 Transformer-base 提升 0.92，比 Performer 提升 2.35。LongBench 上长文问答准确率提升至 73.2%，比基线提升 6.4 个百分点。延迟测试中吞吐提升 1.9 倍。${filler(35)}`);
  parts.push(`表 1：WMT14 主结果。Transformer-base 40.88，Performer 39.45，LoopFormer 41.8。`);
  parts.push(`### 4.3 Ablation Study\n消融实验显示：去掉循环状态更新后 BLEU 掉到 39.1，去掉窗口注意力后掉到 39.7，两者都去掉只剩 37.4。这说明循环状态是主要贡献。${filler(30)}`);
  parts.push(`### 4.4 Efficiency\n在 32k 上下文下，显存占用从 40.2 GB 降到 18.6 GB，训练时间缩短到 3.5 天。`);
  parts.push(`## 5 Discussion and Limitations\n我们的方法只在文本模态上验证，未在语音与多模态上测试；窗口大小 W 需要按任务调参，W 过大时收益消失。此外长文评测只有 3 个数据集，跨语言迁移仍未验证。${filler(40)}`);
  parts.push(`## 6 Conclusion\nLoopFormer 用循环状态替代全局注意力，在保持精度的同时把显存占用降低 53.7%。${filler(20)}`);
  parts.push(`## References\n[1] Vaswani et al. Attention Is All You Need. [2] Choromanski et al. Performer. ${filler(25)}`);
  return parts.join('\n\n');
}

/**
 * 可靠性 / 召回率专用素材：把「主结果表、消融变体、失败案例、局限、伦理、附录」
 * 都放在**后半篇**，用来验证检索不会只看前半篇、以及 audit 的表格数字归一化。
 *
 * 前半篇是方法铺垫（大量 filler），后半篇才是实验与边界结论。
 */
export function reliabilityPaperText() {
  const parts = [];
  parts.push(`## 摘要\n我们提出 Atlas，一个把检索与规划耦合的 agent 框架，在 ToolBench 上把成功率从 41.8% 提到 52.4%。${filler(20)}`);
  parts.push(`## 1 Introduction\n长程任务的成功率受限于规划误差累积，既有做法先规划再执行，误差无法回滚。${filler(40)}`);
  parts.push(`## 2 Method\n### 2.1 Planner\n规划器把任务拆成子目标，并在每一步重新评估剩余预算。${filler(45)}`);
  parts.push(`### 2.2 Executor\n执行器把子目标翻译成工具调用，失败时回退到上一个稳定状态。${filler(45)}`);
  parts.push(`### 2.3 Memory\n记忆模块缓存已经验证过的中间结果，避免重复调用。${filler(45)}`);
  parts.push(`## 3 Experiments\n### 3.1 Setup\n在 ToolBench 与 AgentBench 上评测，基线是 Reflexion 与 ReAct，指标为成功率。${filler(30)}`);
  parts.push(`### 3.2 Main Results\n表 1：ToolBench 主结果。ReAct 41.8%，Reflexion 45.2%，Atlas 52.4%，提升 10.6 个百分点。分列汇总表里三档模型的成功率分别是 05.19%、01.30% 与 02.60%。${filler(35)}`);
  parts.push(`表 2：AgentBench 分域结果。Atlas 在检索类任务上 63.7%，规划类 58.1%，长程任务 44.9%。${filler(25)}`);
  parts.push(`### 3.3 Ablation Study\n消融实验：去掉重规划后成功率掉到 46.1%，去掉记忆缓存后掉到 48.9%，两者都去掉只剩 40.2%。参数敏感性分析显示子目标数超过 6 之后收益消失。${filler(30)}`);
  parts.push(`### 3.4 Model Variations\n模型变体对照：把 planner 换成更小的模型时成功率 49.3%，只保留 w/o memory 变体是 48.9%，说明规划器容量比记忆更关键。${filler(25)}`);
  parts.push(`## 4 Discussion\n结果表明重规划是主要贡献来源，但代价是调用次数增加 1.8 倍。${filler(30)}`);
  parts.push(`## 5 Limitations\n我们的方法只在文本工具上验证，未在多模态工具与真实物理环境中测试；长程任务的评测只有 3 个数据集，跨领域迁移仍未验证。${filler(30)}`);
  parts.push(`## 6 Failure Cases\n失败案例分析：63% 的失败来自工具返回格式异常，21% 来自子目标被错误分解，剩余部分集中在超长任务的预算耗尽。典型失败模式是无法从错误的中间状态恢复。${filler(30)}`);
  parts.push(`## 7 Ethics and Broader Impacts\n能被自动调用工具的 agent 存在滥用风险：可用于批量生成垃圾信息或探测系统边界。我们建议在执行侧加入人工确认与速率限制，并呼吁社区关注安全与更广泛的社会影响。${filler(30)}`);
  parts.push(`## Appendix A Additional Analysis\n附录 A 给出逐任务分解与 unsuccessful attempts：在 12% 的任务上模型无法给出可执行计划，这些任务的成功率接近 0。${filler(30)}`);
  parts.push(`## Appendix B Implementation Details\n附录 B 记录超参、prompt 模板与算力开销。${filler(30)}`);
  parts.push(`## References\n[1] Yao et al. ReAct. [2] Shinn et al. Reflexion. ${filler(20)}`);
  return parts.join('\n\n');
}

/**
 * Retrieval v2 专用素材：**英文 source section 标题**（对应真实 arXiv 论文）+
 * 后半篇的消融 / 失败案例 / 伦理 / 附录内容，用于验证：
 *   - sourceSections 对齐（exact / normalized / acronym）
 *   - mustUseTerms（label smoothing / residual dropout / Table 1 / Figure 1）
 *   - source-local 邻域、槽位分配、跨节多样性
 *   - 检索探针（label smoothing、failure、partial success、unsuccessful）
 */
export function retrievalV2PaperText() {
  const parts = [];
  parts.push(`## Abstract\n我们提出 LoopFormer：用循环状态替代全局注意力，在 WMT14 上 BLEU 达到 41.8。${filler(18)}`);
  parts.push(`## 1 Introduction\n长上下文建模的瓶颈是注意力复杂度，既有做法把上下文截断到 4096 token。${filler(35)}`);
  parts.push(`## 2 Model Architecture\n整体是编码器-解码器骨架，注意力与逐位置前馈交替堆叠。${filler(30)}`);
  parts.push(`### 2.1 Scaled Dot-Product Attention\n注意力把 query 与 key 做点积再归一化：$$h_t = \\alpha \\odot h_{t-1} + (1-\\alpha) \\odot W e_t$$ 其中 alpha 是门控。${filler(30)}`);
  parts.push(`### 2.2 Multi-Head Attention\n多头并行让不同子空间各自建模，输出拼接后投影。${filler(30)}`);
  parts.push(`## 3 Experiments\n### 3.1 Setup\n在 WMT14 英德与英法上评测，基线是 Transformer-base，指标为 BLEU。${filler(25)}`);
  parts.push(`### 3.2 Main Results\n图注：Figure 1 主结果柱状图：LoopFormer 41.8，Transformer-base 40.88。${filler(20)}`);
  parts.push(`表 1：Table 1 主结果对比。Transformer-base 40.88，LoopFormer 41.8。${filler(20)}`);
  parts.push(`## 4 Ablation Study\n消融实验显示：去掉循环状态后 BLEU 掉到 39.1；label smoothing 设为 0.1 时困惑度变好但 BLEU 变差；residual dropout 0.1 是 base 配置的最佳值；variant 对比显示窗口注意力贡献 2.1。${filler(25)}`);
  parts.push(`## 5 Limitations\n我们的方法只在文本模态上验证，未在语音与多模态上测试；窗口大小 W 需要按任务调参。${filler(25)}`);
  parts.push(`## 6 Failure Cases\n失败案例分析（failure cases）：约 21% 的失败来自中间状态被错误覆盖，部分成功（partial success）集中在超长输入。${filler(25)}`);
  parts.push(`## 7 Ethics and Broader Impacts\n长上下文模型存在滥用风险（broader impacts）：可用于批量生成误导性内容，我们建议加入速率限制。${filler(25)}`);
  parts.push(`## Appendix A Additional Analysis\n附录给出 unsuccessful attempts 的分解，以及 AIME 与 MATH 上的补充结果。${filler(25)}`);
  parts.push(`## References\n[1] Vaswani et al. Attention Is All You Need. ${filler(15)}`);
  return parts.join('\n\n');
}

/** arXiv HTML 风格 DOM 片段。 */
export function paperHtml() {
  return `<!doctype html><html><body>
<header><nav>arXiv navigation</nav></header>
<article>
<h1 class="ltx_title_document">LoopFormer: Recurrent Depth for Long Context</h1>
<section class="ltx_section"><h2 class="ltx_title_section">1 Introduction</h2>
<p>Long context modeling is limited by attention cost.</p>
<p>We study how to keep a global state under a fixed memory budget, using <math><annotation encoding="application/x-tex">O(L)</annotation></math> per token.</p>
</section>
<section class="ltx_section"><h2 class="ltx_title_section">2 Method</h2>
<p>The method has a recurrent state update and a windowed attention layer.</p>
<p>State update: <math><annotation encoding="application/x-tex">h_t = \\alpha \\odot h_{t-1} + (1-\\alpha) \\odot W e_t</annotation></math> where alpha is the gate.</p>
<figure><figcaption>Figure 1: Architecture of LoopFormer with recurrent state.</figcaption></figure>
<figure class="ltx_table"><figcaption>Table 1: Results on WMT14 English-German.</figcaption><table><tr><td>LoopFormer 41.8 BLEU</td></tr></table></figure>
</section>
<section class="ltx_section"><h2 class="ltx_title_section">3 Experiments</h2>
<p>We evaluate on WMT14 and LongBench. BLEU reaches 41.8 and accuracy reaches 73.2%.</p>
<p>Ablation: removing the recurrent update drops BLEU to 39.1.</p>
</section>
<section class="ltx_section"><h2 class="ltx_title_section">4 Limitations</h2>
<p>Only text modality is tested, and the window size needs tuning.</p>
</section>
</article></body></html>`;
}

/** latexToText() 产物形态。 */
export function paperTex() {
  return [
    '## 摘要',
    `我们提出 LoopFormer，在 WMT 上 BLEU 达到 41.8。${filler(6)}`,
    '## 1 Introduction',
    `长上下文建模的瓶颈在于注意力复杂度。${filler(8)}`,
    '## 2 Method',
    `状态更新算子把历史状态带入当前计算。${filler(8)}`,
    '$$h_t = \\alpha \\odot h_{t-1} + (1-\\alpha) \\odot W e_t$$',
    '图注：图 1 架构图，包含循环状态与窗口注意力',
    '## 3 Experiments',
    `主结果 BLEU 41.8，消融去掉循环状态后掉到 39.1。${filler(8)}`,
    '## 4 Limitations',
    `只在文本模态验证，窗口大小需要调参。${filler(8)}`,
  ].join('\n\n');
}

/** PDF 抽出的带换行正文。 */
export function paperPdfLines() {
  return [
    'LoopFormer: Recurrent Depth for Long Context',
    'Yifan Zhang',
    'Abstract',
    'We propose LoopFormer, reaching 41.8 BLEU on WMT14.',
    '1 Introduction',
    'Long context modeling is limited by attention cost.',
    '2 Method',
    'The state update operator carries the previous state into the current step.',
    '3 Experiments',
    'Main results: 41.8 BLEU. Ablation: without the recurrent update, BLEU drops to 39.1.',
    '4 Limitations',
    'We only test the text modality and the window size needs tuning.',
    'References',
    '[1] Vaswani et al.',
  ].join('\n');
}

/** 极简但合法的单页 PDF（供 HTML → TeX → PDF 回退测试用）。 */
export function minimalPdf() {
  const lines = [
    'LoopFormer Recurrent Depth for Long Context',
    'Abstract We propose LoopFormer and report 41.8 BLEU on WMT14 English German translation.',
    '1 Introduction Long context modeling is limited by the quadratic cost of attention and by the memory wall.',
    '2 Method The state update operator carries the previous state into the current step with a gate.',
    '3 Experiments Main results show 41.8 BLEU and accuracy 73.2 percent on the long benchmark suite.',
    '4 Limitations We only test the text modality, and the window size needs tuning per task.',
  ];
  const content = lines.map((l, i) => `BT /F1 10 Tf 40 ${740 - i * 16} Td (${l.replace(/[()\\]/g, '')}) Tj ET`).join('\n');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let pdf = '%PDF-1.4\n';
  const offsets = [];
  objects.forEach((body, i) => {
    offsets.push(pdf.length);
    pdf += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const off of offsets) pdf += `${String(off).padStart(10, '0')} 00000 n \n`;
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf, 'latin1');
}

/** 脚本化 chat：按最后一次 user 消息内容返回不同内容，模拟模型的多阶段输出。 */
export function scriptedChat({ onCall, planText, researchMapJson, sectionText, repairText } = {}) {
  return async (messages) => {
    const user = messages[messages.length - 1]?.content || '';
    const sys = messages[0]?.content || '';
    onCall?.({ user, sys });
    if (/论文地图 JSON/.test(user)) {
      return { content: researchMapJson ?? JSON.stringify({
        problem: '长上下文建模的注意力开销',
        key_claims: [{ text: '循环状态可以替代全局注意力', chunkIds: ['c1'] }],
        method_components: [{ text: '状态更新算子 h_t = alpha*h_{t-1} + (1-alpha)*W*e_t', chunkIds: ['c2'] }],
        equations: [{ text: 'h_t = \\alpha \\odot h_{t-1} + (1-\\alpha) \\odot W e_t', chunkIds: ['c2'] }],
        datasets: [{ text: 'WMT14', chunkIds: ['c3'] }],
        benchmarks: [{ text: 'BLEU', chunkIds: ['c3'] }],
        baselines: [{ text: 'Transformer-base', chunkIds: ['c3'] }],
        main_results: [{ text: 'BLEU 41.8，准确率 73.2%', chunkIds: ['c3'] }],
        ablations: [{ text: '去掉循环状态 BLEU 掉到 39.1', chunkIds: ['c4'] }],
        limitations: [{ text: '只在文本模态验证', chunkIds: ['c5'] }],
        figures: [{ num: 1, caption: 'Architecture of LoopFormer', sectionTitle: 'Method', chunkIds: ['c6'] }],
        evidence: [{ text: '主结果 41.8 BLEU', chunkIds: ['c3'] }],
      }) };
    }
    if (/请给出大纲/.test(user)) {
      return { content: planText ?? [
        '## 为什么长上下文需要循环状态｜导语：回答什么问题',
        '## 旧做法卡在注意力开销｜动机与产物',
        '## 状态更新怎么从输入走到输出｜机制 + 最小例子',
        '## 41.8 BLEU 与消融说明了什么｜证据（图1）',
        '## 只在文本模态验证｜边界与追问',
      ].join('\n') };
    }
    if (/只重写这一节|修订编辑/.test(`${sys}\n${user}`)) {
      return { content: repairText ?? '## 41.8 BLEU 与消融说明了什么｜证据\n\n修订后：BLEU 41.8，去掉循环状态掉到 39.1，显存 18.6 GB。' };
    }
    if (/请撰写第/.test(user)) {
      const m = sys.match(/作为本节的 Markdown H2）：##\s*(.+)/);
      const title = m ? m[1].trim() : '小节';
      const body =
        sectionText ??
        '正文：BLEU 41.8，准确率 73.2%，消融去掉循环状态掉到 39.1，只在文本模态验证。公式 $h_t = \\alpha \\odot h_{t-1}$。';
      return { content: `## ${title}\n\n${body}` };
    }
    return { content: '' };
  };
}
