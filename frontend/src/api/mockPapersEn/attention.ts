import type { MockAnalysis, MockScriptSegment } from '../mockPapers'
import type { MockPaperMeta } from '../mockPapers'

/** Attention Is All You Need 的英文版解读与双人播客脚本（Mock 双语演示用） */
export const EN_ANALYSIS: MockAnalysis = {
  background:
    'Before 2017, sequence-to-sequence modeling was owned by RNNs and LSTMs. Those models compute one time step at a time: the hidden state at step t depends on step t-1, so the GPU mostly sits idle, and the information path between any two positions is O(n), which means long-range dependencies get diluted somewhere in the middle. Bahdanau attached attention to an encoder-decoder in 2014 to ease the alignment problem, but it stayed a helper module bolted onto a recurrent network. Convolutional routes like ByteNet can parallelize, yet two distant positions still sit O(log n) layers apart. The paper asks one blunt question: can attention alone perform sequence transduction?',
  innovations: [
    'A Transformer built entirely from attention, with six encoder and six decoder layers, no recurrence and no convolution, so every position in a layer is computed in parallel and training time stops growing linearly with sequence length.',
    'Scaled dot-product attention, which divides the Q-K dot product by the square root of d_k before the softmax, so scores do not blow up as the dimension grows and push the softmax into a saturated region with tiny gradients.',
    'Eight attention heads computed in parallel, each projecting the 512-dimensional representation into a 64-dimensional subspace so the model can attend to syntax, coreference and positional relations separately, before the heads are concatenated back to the original width.',
    'Fixed sinusoidal position encodings, which add no learnable parameters and make relative offsets expressible as a linear transformation; experiments show they perform on par with learned positional embeddings.',
    'A training recipe of Adam with 4,000 linear warmup steps then square-root decay, dropout of 0.1 and label smoothing of 0.1, which keeps a deep stack converging stably without BatchNorm.',
  ],
  method:
    'Every token is first looked up as a 512-dimensional d_model vector, then position encoding is added. Each encoder layer has two sublayers: multi-head self-attention, then a two-layer feed-forward network with an inner dimension of 2048, and both sublayers are wrapped in a residual connection plus layer normalization. The decoder adds a cross-attention sublayer where the queries come from the decoder output of the previous step and the keys and values come from the encoder; its self-attention is masked, setting the weights of future positions to negative infinity so position t can only see what came before it. Scaled dot-product attention is a softmax of Q times K transpose divided by the square root of d_k, multiplied by V: matrix multiplication, a scaling, a softmax, no recurrent dependency. Multi-head attention projects Q, K and V through separate matrices, splits into eight heads computed in parallel with d_k and d_v both equal to 64, concatenates back to 512 dimensions and projects once more.',
  experiments:
    'WMT 2014 English-German and English-French were the main battleground. Base trained 12 hours on eight P100s and big trained 3.5 days. English-German hit 28.4 BLEU, more than 2 BLEU above the best prior ensemble; English-French reached 41.8 BLEU with a single model. Cutting eight heads to one head cost 0.9 BLEU, and cutting head dimension from 64 to 32 cost 0.7 BLEU. On WSJ constituency parsing they reached F1 92.7 against a previous best of 91.7.',
  conclusion:
    'The conclusion is that sequence transduction does not need recurrence. The Transformer set a new state of the art on translation while training more cheaply, and more importantly it shortened the interaction path between any two positions to a constant and organized computation into matrix multiplies that can be laid out across the hardware. That is what finally made it practical to keep scaling the model up. The authors offered attention visualizations as corroborating evidence: different heads really did specialize, some tracking adjacent words, others following verb-object relations or resolving syntactic dependencies.',
  limitations: [
    'Self-attention is quadratic in sequence length: encoding 2,000 tokens already means four million relevance scores, so memory and compute both get expensive on long documents and long audio.',
    'Sinusoidal position encodings extrapolate poorly, so performance drops once the input is clearly longer than the training length, and the paper never systematically tested very long inputs.',
    'The largest model was about 213 million parameters and the training data stayed at the level of a few million sentence pairs, so the paper says nothing about what happens as parameters and data keep growing; scaling laws were studied properly only later.',
    'Evaluation leans on BLEU, which is insensitive to semantic equivalence, pronoun resolution and named entities, so it cannot catch output that is fluent but semantically off.',
  ],
  value:
    'It turned NLP training from stepping through a sentence token by token into batched matrix multiplication, and mainstream frameworks have poured enormous engineering into that computation graph: fused attention kernels, tensor parallelism, mixed precision and KV caching all sit on this foundation. Any model that handles long sequences today, whether the modality is text, speech or images, uses some version of its encoder or decoder, and it is the shared substrate under the BERT, GPT and T5 generation of pretrained models.',
  future: [
    'In vision, ViT cuts an image into 16x16 patches and treats them as tokens, and Swin brings complexity down to linear with windowed attention, showing the architecture is not limited to language.',
    'On efficiency, FlashAttention provides IO-aware exact attention while Longformer and BigBird use sparse attention, all aimed at the quadratic cost of long sequences.',
    'On scale, the GPT line pushed forward, Kaplan and colleagues gave scaling laws, and Chinchilla corrected the ratio between parameters and training data, taking models from hundreds of millions to hundreds of billions.',
    'Architecturally, Mamba replaces attention with a state space model, and hybrid designs mix attention with convolution or recurrence, all trying to recover linear cost on very long sequences.',
  ],
}

export const EN_SCRIPT: MockScriptSegment[] = [
  {
    speaker: 'A',
    text: "Today's paper is Attention Is All You Need, published at NeurIPS 2017 by a Google team, arXiv 1706.03762, first author Ashish Vaswani, eight authors in total. The Transformer it introduced became the backbone of essentially every large model since.",
  },
  {
    speaker: 'B',
    text: 'Let me check one premise first. Back in 2017 machine translation was still dominated by LSTMs with attention bolted on, right? So which layer did this paper actually rip out, and is it really worth all this fuss? LSTMs had been the standard answer for long dependencies for years.',
  },
  {
    speaker: 'A',
    text: 'It went after the foundation. In earlier encoder-decoder stacks attention was only a helper module hanging off a recurrent network. This paper deletes recurrence entirely, leaving nothing but attention and feed-forward layers, which is why the title dares to say attention is all you need.',
  },
  {
    speaker: 'B',
    text: 'Then why was deleting it so necessary? RNNs had handled speech recognition and machine translation well for years, and my impression is that LSTMs were invented specifically to solve long dependencies. There has to be a solid reason behind the claim.',
  },
  {
    speaker: 'A',
    text: "The problem is speed and path length. Step t cannot start until the hidden state at step t-1 is finished, so a 50-word sentence means 50 serial computations and the GPU's parallelism goes unused. Worse, two distant words have to pass information through dozens of steps, and gradients get diluted along the way. Convolutional routes like ByteNet can parallelize, but any two positions still sit O(log n) to O(n) layers apart.",
  },
  {
    speaker: 'B',
    text: 'Let us get into the mechanism. People always say this attention captures the whole context in a single dot product. Can you drop the notation and explain in plain words what it is actually doing, and why it is better at long-range relations than recurrence?',
  },
  {
    speaker: 'A',
    text: 'Think of it as a dictionary lookup. Each word produces three vectors: Q is what I am looking for, K is what I can be matched by, and V is the content I actually carry. The current word dots its Q against every K to get relevance scores, normalizes them with a softmax, and uses those weights to sum up all the V. No matter how far apart two words are, they are one dot product away.',
  },
  {
    speaker: 'B',
    text: 'What is that division by the square root of d_k doing in the formula? I have seen it in implementation after implementation but never understood why it is mandatory. Is it about numeric overflow, or is it about gradients?',
  },
  {
    speaker: 'A',
    text: 'Because the variance of the dot product grows linearly with the dimension. When d_k is 64 the scores swing widely, softmax gets pushed into a very peaked distribution, and the backpropagated gradients are close to zero. Dividing by the square root of d_k pulls the variance back near one, and that is what keeps training stable.',
  },
  {
    speaker: 'B',
    text: 'Is one attention head not enough? Why split it into eight heads running in parallel? Does that not weaken what each head can express? My worry is that squeezing 512 dimensions down to 64 throws information away.',
  },
  {
    speaker: 'A',
    text: 'It is a division of labor. The eight heads each learn their own projection, squeeze 512 dimensions into 64, compute attention in different subspaces, and then concatenate back to 512. When the authors plotted the attention maps, some heads were clearly tracking adjacent words while others followed verb-object relations.',
  },
  {
    speaker: 'B',
    text: 'So what about position? From your description attention looks at every word simultaneously and word order never enters the computation. How does the model know that a cat chasing a dog is not the same thing as a dog chasing a cat?',
  },
  {
    speaker: 'A',
    text: 'That is exactly why position encodings have to be added separately. They use a set of sine and cosine functions at different frequencies, added to the word vectors according to position. The form has two advantages: it adds no learnable parameters, and relative offsets can be expressed as a linear transformation, so relations like a few words apart are easy for the model to learn.',
  },
  {
    speaker: 'B',
    text: 'How are the encoder and decoder stacked? I vaguely remember the decoder has one more attention sublayer than the encoder, so who is querying whom there? And while you are at it, explain what the mask is for.',
  },
  {
    speaker: 'A',
    text: "Six layers on each side, and every sublayer is wrapped in a residual connection plus layer normalization. An encoder layer is multi-head self-attention followed by a feed-forward network that goes 512 to 2048 and back to 512. The decoder adds cross-attention, where Q comes from the decoder's own output and K and V come from the encoder; its self-attention is masked, so position t can only see the words before it.",
  },
  {
    speaker: 'B',
    text: 'Any interesting training details? The part I remember best is the learning rate warmup, rising linearly for 4,000 steps and then decaying. That looks deliberately designed rather than a constant someone picked at random.',
  },
  {
    speaker: 'A',
    text: 'It was deliberately designed. Adam with 4,000 warmup steps, then square-root decay by step count, plus dropout of 0.1 and label smoothing of 0.1. It looks fussy, but stacking six layers deep and converging stably without BatchNorm is exactly what that combination buys you.',
  },
  {
    speaker: 'B',
    text: 'Give me a few numbers from the experiments that I can actually remember, the kind I could repeat to someone else afterwards. And then tell me about the limitations. I do not want to finish an episode remembering only that it worked well.',
  },
  {
    speaker: 'A',
    text: 'English-German: 28.4 BLEU, over 2 points above the best ensemble at the time. English-French: 41.8 BLEU from a single model, trained on eight P100s for 3.5 days, while the base model needed just 12 hours. And cutting eight heads down to one dropped BLEU by 0.9, so multi-head attention is not decoration.',
  },
  {
    speaker: 'B',
    text: 'What about the limitations? This paper is almost canonized now. If you had to walk it back, what is the most important thing to point out? Is it compute, is it evaluation, or something else that people tend to overlook?',
  },
  {
    speaker: 'A',
    text: 'First, complexity. Self-attention is quadratic in sequence length, so 2,000 tokens already means four million relevance scores, which is rough for long documents and long audio. Position encodings also extrapolate poorly, so performance drops once the sequence runs longer than training. And evaluation leans on BLEU, which is not sensitive to semantic equivalence.',
  },
  {
    speaker: 'B',
    text: 'Then wrap it up for our listeners. A translation paper from 2017, why does it still deserve a full episode today? Where exactly did its influence land, and what is still worth knowing for people building systems right now?',
  },
  {
    speaker: 'A',
    text: 'Because it found a fully parallel path for sequence modeling. Path length went from O(n) to a constant, training went from token by token to batched matrix multiplication, and that is what made it possible to scale to hundreds of billions of parameters. BERT, GPT and T5 all grew on this skeleton, and almost every model that handles long sequences today runs on the computation graph this paper defined.',
  },
]

/**
 * 英文版论文元信息里**分语言**的那部分：摘要与关键词。
 * 标题 / 作者 / 年份 / 会议 / arXiv 编号两种语言共用（本来就是英文），不在这里。
 */
export const EN_META: Pick<MockPaperMeta, 'abstract' | 'keywords'> = {
  abstract:
    'The paper proposes the Transformer, a sequence transduction architecture built entirely on attention that removes recurrence and convolution from the backbone altogether. The encoder and decoder each stack six layers, and every layer is made of multi-head self-attention and a feed-forward network. On WMT 2014 English-to-German translation it reaches 28.4 BLEU, more than 2 BLEU above the previous best ensemble result, and on English-to-French a single model reaches 41.8 BLEU while training for only 3.5 days on eight P100 GPUs. Attention visualizations show that different heads specialize in tracking adjacent words or in capturing syntactic dependencies.',
  keywords: [
    'Transformer',
    'Self-Attention',
    'Machine Translation',
    'Sequence-to-Sequence Modeling',
    'Multi-Head Attention',
  ],
}
