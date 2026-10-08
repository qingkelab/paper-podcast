import type { MockAnalysis, MockScriptSegment } from '../mockPapers'
import type { MockPaperMeta } from '../mockPapers'

/** LoRA: Low-Rank Adaptation 的英文版解读与双人播客脚本（Mock 双语演示用） */
export const EN_ANALYSIS: MockAnalysis = {
  background:
    'Back in 2021, if you wanted to move a pretrained model like BERT or GPT onto a downstream task, the default move was still full fine-tuning: update every weight for every task and keep a separate copy of the model for each one. GPT-3 has 175 billion parameters, and a single fp16 copy of those weights is 350 gigabytes. The other camp trained only a handful of parameters instead. Adapters insert a small bottleneck module into every layer; prefix tuning prepends learnable vectors to the input. Both cut trainable parameters dramatically, but both also change the architecture, and adapters pile inference cost on top, roughly a 21 percent latency increase. So the question became: can we leave the architecture alone, add nothing to inference, and still push trainable parameters down to the one-in-ten-thousand range?',
  innovations: [
    'Low-rank reparameterization: split the weight update into two small matrices, B times A, which drops the parameter count from d times k down to r times (d plus k). With r between 1 and 16, trainable parameters fall into the one-in-ten-thousand range.',
    'Initialize B to zeros and A with Gaussian noise, so training starts as an exact identity. The model output matches the pretrained weights precisely, which avoids the disruption adapters cause early in training.',
    'The adaptation can be merged back into the original weights: at inference you just add B times A onto W. Neither the architecture nor the forward-pass latency changes, which is a fundamental difference from adapters from a deployment standpoint.',
    'A subspace similarity analysis explains why the low-rank assumption holds: the directions that shift the most during adaptation get covered at very small rank, so the degrees of freedom a task actually needs sit far below the raw parameter count.',
    'The paper ablates where to apply it. Adapting the attention projections, and putting it on Q and V at the same time, works best; from there, adding more rank or more layers runs into diminishing returns fast.',
  ],
  method:
    'Take a pretrained weight W0 and write the fine-tuned forward pass as h equals W0 x plus delta-W x. LoRA sets delta-W to B times A, where B is d by r and A is r by k, with r far smaller than either d or k. B starts as zeros and A as random Gaussian values, so delta-W is zero at step one and the model behaves exactly like the pretrained weights. The extra forward-pass compute is on the order of r times (d plus k), which is negligible. Gradients only flow into these projection matrices; everything else is frozen, so the optimizer state covers just that small slice of parameters and memory drops sharply. The update is scaled by alpha over r, which means you can change r without retuning the learning rate. At deployment you can fold B times A into a single product matrix and add it onto W0, leaving the forward graph identical, so there is no extra inference latency.',
  experiments:
    'The paper validates at three scales. GPT-2 Medium has 355 million parameters; on E2E natural language generation, full fine-tuning scores 68.2 BLEU while LoRA at r equals 4 reaches 70.4 with only 0.35 million trainable parameters, about one thousandth of the full model. On GLUE, RoBERTa and DeBERTa match or slightly beat full fine-tuning. On GPT-3 175B, trainable parameters drop to 4.7 million and checkpoints shrink from 350GB to 35MB. On latency, adapters run at about 1.21 times the baseline, while LoRA merged back into the weights matches the baseline exactly.',
  conclusion:
    'The conclusion is that fine-tuning a large model does not have to mean updating every weight. Task adaptation simply needs far less freedom than the total parameter count suggests. Constrain the update to a low-rank space and you can match full fine-tuning quality while cutting trainable parameters, optimizer state and storage by orders of magnitude, all without adding inference latency. The paper moves the bar for fine-tuning large models from needing a cluster on par with the pretraining run down to possibly getting by on one card with a reasonable amount of memory.',
  limitations: [
    'The boundaries of the low-rank assumption are unclear. Experiments mostly cover generation and classification tasks close to the pretraining distribution, and the gains are limited when the domain shifts far or the task demands genuinely new knowledge.',
    'There is no universal recipe for the three hyperparameters: rank, scaling factor and which layer to adapt. The ablations only give directional guidance, so real projects still have to try each setting one by one.',
    'Evaluation centers on small and mid-sized models and short-sequence generation. There is no analysis of forgetting or interference under continual multi-task fine-tuning, and no answer for what happens when several adapters are stacked.',
    'It leaves the base model untouched, so it cannot push past the base model capability ceiling. Keeping knowledge current still means retraining the base.',
  ],
  value:
    'LoRA lets one set of base weights serve dozens of tasks: each task stores only a tens-of-megabytes adapter file, and you hot-swap them at load time instead of deploying a full model per task. In engineering terms that changes two things outright. First, hardware: teams that would have needed a pile of GPUs for multi-task inference can now run one card plus a bundle of adapters. Second, iteration speed: training an adapter costs one to two orders of magnitude less than full fine-tuning, so the data annotation loop can turn around in days. Today it is essentially the default in the open-source fine-tuning workflow.',
  future: [
    'QLoRA quantizes the base to 4-bit and stacks LoRA on top, which lets you fine-tune a 65-billion-parameter model on a single 48GB GPU. It pushes the hardware bar lower still, and it is the most widely used recipe in the community.',
    'AdaLoRA replaces the fixed rank budget with dynamic allocation based on importance, while DoRA decouples the magnitude of the weight update from its direction. Both are answering the same question: which weights deserve the rank you have.',
    'Composing and managing adapters has become a topic in its own right. Model merging, stacking several adapters and routing across tasks are gradually turning LoRA from a fine-tuning trick into a new way of distributing models.',
    'The same idea has been carried over to diffusion models and vision tasks. Pairing LoRA with DreamBooth, for instance, customizes a style or a subject from a handful of images at a fraction of the cost of retraining.',
  ],
}

export const EN_SCRIPT: MockScriptSegment[] = [
  {
    speaker: 'A',
    text: 'This episode is LoRA, Low-Rank Adaptation of Large Language Models. It went up on arXiv in June 2021 as 2106.09685, with eight authors from Microsoft including Edward Hu, Yelong Shen and Weizhu Chen, and it was published at ICLR in 2022.',
  },
  {
    speaker: 'B',
    text: 'The headline I remember is that it cuts trainable parameters by a factor of ten thousand. Where does that number actually come from? Is it about GPT-3 with 175 billion parameters, or some smaller model? I have never been clear on what the baseline is for that claim.',
  },
  {
    speaker: 'A',
    text: "The setting is fine-tuning a 175-billion-parameter model like GPT-3. The old way was full fine-tuning: every downstream task needed its own complete copy of the weights, and one fp16 copy is 350 gigabytes. You swap models whenever you swap tasks, which is basically unworkable to deploy. LoRA says you don't have to touch the original weights.",
  },
  {
    speaker: 'B',
    text: "If you don't touch the original weights, how does the model learn anything new? Is it adding a small adapter network on the outside, the way the adapter line of work did? And if it is, what makes it fundamentally different from what came before?",
  },
  {
    speaker: 'A',
    text: "It goes further than adapters. The hypothesis is that the weight update fine-tuning produces is itself low-rank. Adapting to a task doesn't need to occupy the whole parameter space, so they factor the update into a product of two small matrices — one d by r, one r by k — and an r of 4 or 8 is small enough.",
  },
  {
    speaker: 'B',
    text: 'Why is it safe to assume the update is low-rank? That sounds like a strong claim to me. What if the task is genuinely complicated and needs to move many directions at once? Is there any empirical basis for it? I have trouble believing it holds in general.',
  },
  {
    speaker: 'A',
    text: 'The paper runs an empirical analysis. They compare full fine-tuning against randomly projected subspaces and find that the directions that change the most during adaptation are already covered at very small rank. Push r from 1 up to 64 and the performance curve flattens out quickly, which says the degrees of freedom a task needs are far smaller than the parameter count implies.',
  },
  {
    speaker: 'B',
    text: 'So how are those two small matrices initialized? Does training not wreck what pretraining learned right at the start? My worry is that the outputs drift immediately, since the very first steps already move the weights. Did the paper address that point specifically?',
  },
  {
    speaker: 'A',
    text: "The trick is that B is initialized to all zeros and A is random Gaussian. At step one B times A is a zero matrix, so the model output is exactly identical to the original model, which means it trains smoothly from an identity state and the pretrained knowledge never gets damaged.",
  },
  {
    speaker: 'B',
    text: "So the forward pass is the original weight plus the product of those two small matrices? Does that extra step slow inference down? I've heard adapters have exactly that problem, stringing an extra computation through every single layer.",
  },
  {
    speaker: 'A',
    text: "During training it really is W plus B times A, times x, so there is one extra small matmul and the cost is tiny. At inference you can precompute B times A and add it straight onto W, leaving the architecture completely unchanged and the added latency at zero. Adapters thread an extra network through every layer, so they always slow inference down — the paper measures roughly 21 percent more.",
  },
  {
    speaker: 'B',
    text: "With that many fewer trainable parameters, can it actually match full fine-tuning? That's the thing I most want confirmed. If quality drops noticeably, then saving resources means much less. Does the paper give a direct head-to-head comparison?",
  },
  {
    speaker: 'A',
    text: "The paper's answer is that it matches, and sometimes slightly exceeds. On E2E natural language generation, full fine-tuning of GPT-2 Medium scores 68.2 BLEU with 355 million trainable parameters; LoRA at r equals 4 gets 70.4 with only 350 thousand, a factor of a thousand apart. GLUE and summarization land on the same conclusion.",
  },
  {
    speaker: 'B',
    text: 'And what does that look like concretely on GPT-3 175B? I want to know how much hardware you actually save, since those numbers carry more weight than the small-model ones. While you are at it, explain why memory drops so much.',
  },
  {
    speaker: 'A',
    text: 'Trainable parameters go from 175 billion down to 4.7 million, checkpoints shrink from 350GB to 35MB, and memory demand falls to roughly a third. The overall numbers the paper reports: up to ten thousand times fewer trainable parameters, and three times less memory.',
  },
  {
    speaker: 'B',
    text: 'Can you spell out the memory side? Why does having fewer trainable parameters cut memory that much? Those feel like two different things to me. Where is the real cost during training, anyway?',
  },
  {
    speaker: 'A',
    text: 'Because full fine-tuning has to store gradients, first-order moments and second-order moments. For 175 billion parameters under Adam, the optimizer state alone runs into the thousands of gigabytes, which no single machine can hold. LoRA keeps that state only for the small slice of parameters, so the real requirement drops into the tens to hundreds of gigabytes — and that is what made fine-tuning a model like GPT-3 practical for the first time.',
  },
  {
    speaker: 'B',
    text: 'Are there practical conclusions about the mechanics? Like which layer is the best value to apply LoRA to, and whether a bigger rank is always better? These are decisions you have to make in a real project. Does the paper recommend a configuration?',
  },
  {
    speaker: 'A',
    text: 'The paper does run ablations. At the same parameter budget, applying it to both Q and V in attention works best; those two projections alone are enough, and covering every weight matrix actually gets worse value for the parameters. The alpha over r scaling has to be tuned along with it, and bigger rank is not better — past a certain value the gains stop.',
  },
  {
    speaker: 'B',
    text: "So what's wrong with it? I don't want a pitch that only lists the upside. What do you actually trip over in practice, and where does it simply not apply? I especially want to know what it cannot do, and where the boundary sits.",
  },
  {
    speaker: 'A',
    text: "At least four things. It is unclear which tasks the low-rank assumption actually holds for, and it wants a downstream distribution reasonably close to pretraining. There is no universal recipe for rank, alpha, or which layer to adapt, so you can only try things. It cannot inject new knowledge into the model; it activates what is already there, so tasks with a big domain shift see limited gains. And evaluation focuses on short-sequence generation, with no measurement of interference under long-running multi-task fine-tuning.",
  },
  {
    speaker: 'B',
    text: 'Then who filled those gaps afterwards? If a listener wants to keep reading, which papers should they pick up? Ideally the ones you could already use today. And tell me what problem each of them solves.',
  },
  {
    speaker: 'A',
    text: 'QLoRA quantizes the base to 4-bit and stacks LoRA on top, letting you fine-tune a 65-billion-parameter model on a single 48GB card. AdaLoRA turns the fixed rank budget into importance-based dynamic allocation, and DoRA decouples magnitude from direction. Today it is also the de facto standard for fine-tuning in the open-source community.',
  },
]

/**
 * 英文版论文元信息里**分语言**的那部分：摘要与关键词。
 * 标题 / 作者 / 年份 / 会议 / arXiv 编号两种语言共用（本来就是英文），不在这里。
 */
export const EN_META: Pick<MockPaperMeta, 'abstract' | 'keywords'> = {
  abstract:
    'The paper proposes LoRA, which constrains the weight updates introduced by downstream task adaptation to a low-rank subspace: the pretrained weights are frozen, two small matrices A and B are inserted for each layer, and their product represents the weight update. On GPT-3 175B, trainable parameters drop from 175 billion to 4.7 million and checkpoints shrink from 350GB to 35MB, and the paper reports an overall reduction of up to 10,000 times in trainable parameters and 3 times in GPU memory. At inference the two small matrices can be merged back into the original weights, so there is no extra latency.',
  keywords: [
    'LoRA',
    'Low-Rank',
    'Parameter-Efficient Fine-Tuning',
    'Large Language Models',
    'Adapter',
    'GPT-3',
  ],
}
