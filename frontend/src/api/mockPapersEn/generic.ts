import type { MockAnalysis, MockScriptSegment } from '../mockPapers'
import type { MockPaperMeta } from '../mockPapers'

/** 通用兜底模板（图神经网络 / 分子性质预测）的英文版解读与双人播客脚本（Mock 双语演示用） */
export const EN_ANALYSIS: MockAnalysis = {
  background:
    'Across many application areas, the early default was to compress every sample into a fixed-length feature vector and hand it to a gradient-boosted tree. Those features are counts of local units, so they throw away how the units connect to one another, and they throw away geometry as well. When two samples are structurally similar but differ sharply in the property you care about, a model of this kind usually cannot tell them apart. In 2018, MoleculeNet collected roughly 700,000 samples and published the first public benchmark, but papers disagreed on data splits and hyperparameter budgets. Run the same model under a different protocol and it moves by several points, which makes published conclusions impossible to compare side by side. That gives this work a very concrete question: of the gains reported on those leaderboards, how much came from the model itself, and how much came from the evaluation protocol?',
  innovations: [
    'It establishes a unified evaluation protocol and reruns every baseline under the same splits, the same input features and a fixed tuning budget, which exposes the several points of inflated performance that split differences had been hiding.',
    'It compares two technical routes, structured modeling and pretraining, head to head, and finds that the payoff from pretraining depends heavily on how many downstream labels you have: once labels are plentiful, that gain shrinks to within a single point.',
    'It proposes a two-channel encoder that fuses two input representations, so samples with no geometric information can still benefit from structured features. On geometry-sensitive tasks it beats a single-channel model by three to five points.',
    'It designs a sample-level self-supervised objective that combines masked attribute prediction with contrastive learning. Pretraining runs on 11 million unlabeled samples, which is what closes the capability gap in label-scarce settings where annotation is expensive.',
    'It reports a compute-versus-performance curve showing that returns stop once the number of aggregation layers passes five, while pretraining buys more at the same compute cost, giving engineering teams a direct basis for their design choices.',
  ],
  method:
    'Every sample is represented as an attributed structure: the units are nodes, the connections between them are edges, node features carry type, degree and whether the node sits on a cycle, and edge features carry connection type and strength. The core operation is layer-wise aggregation. At layer l, each node first sums the messages coming from its neighbours into a single vector, concatenates that with its own state, and passes the result through a linear transform and a nonlinearity to get its new representation. Summation is the default aggregator because it is insensitive to node degree and because its discriminative power lines up exactly with the classical Weisfeiler-Lehman test; swapping in mean or max pooling measurably weakens that power. Pushing information across longer distances means stacking more layers, but past a certain depth everything smooths out and all node representations converge. The model therefore uses five aggregation layers with skip connections, concatenating the per-layer representations before readout. A final attention-weighted readout avoids letting one oversized sample dominate a batch, and a two- or three-layer perceptron head turns the pooled vector into a prediction.',
  experiments:
    'On ogbg-molhiv, ROC-AUC climbs from the previous best baseline of 0.7558 to 0.826. On the larger ogbg-molpcba, average precision moves from 0.2266 to 0.291, and more than ninety percent of its 128 subtasks improve. On regression, the average MAE over the 12 QM9 targets comes down from 0.045 to 0.031. In the low-label regime, with only 500 training labels, the pretrained model runs 8 to 12 points ahead of random initialization. The ablations are just as informative: dropping the geometry channel costs three points on the tasks that rely on it, and increasing aggregation depth from five layers to eight loses 1.5 points instead of gaining.',
  conclusion:
    'The conclusion lands on two levels. On the method side, the expressive power of layer-wise aggregation is capped by what you can build out of a handful of neighbourhoods; going deeper is less useful than finding a better pretraining signal, and the real gains concentrate on small datasets where labels are scarce. On the evaluation side, a good part of the performance that earlier leaderboards reported came from differences in data splits and hyperparameter budgets rather than from model architecture. Both point at the same thing: the bottleneck in this task has moved from structural design to data and evaluation standards.',
  limitations: [
    'The expressive ceiling of layer-wise aggregation is bounded by the classical discriminative test it corresponds to, which is not enough for tasks that hinge on geometric detail, and the paper offers no architectural design that breaks through that ceiling.',
    'The 11 million pretraining samples sit inside a single data distribution, so the gains decay sharply once you transfer out of distribution, and the paper never runs a cross-domain validation to measure how bad that decay gets.',
    'Structure-grouped splits are stricter than random splits, but they still cannot simulate the temporal drift of a real deployment, and the model performs poorly on samples that are structurally similar yet differ abruptly in the target property.',
    'Every number rests on consistency across public datasets, with no prospective validation in a real setting, so there is still a gap between the reported performance gains and the hit rate you would actually observe.',
  ],
  value:
    'In engineering terms, the most direct value is a reproducible baseline for design choices. The gain curves for aggregation depth, input features and pretraining data volume can be read straight off and used to fix an architecture, instead of rerunning the whole literature yourself. For teams short on data, the edge that a pretrained model holds on small-sample tasks lands exactly where real projects live, since in-house annotation typically amounts to a few hundred or a few thousand examples while public unlabeled data is free and abundant. That protocol is also the default comparison standard for work in this area today.',
  future: [
    'Geometrically equivariant networks have become the main line of work: SchNet, DimeNet++ and GemNet write symmetry directly into the model and run equivariant convolutions on coordinates, filling in the information that two-dimensional structural representations leave out.',
    'Pretraining keeps scaling up. Published work already pretrains on 19 million samples and 209 million geometric configurations, while another line pushes the training set to 1.1 billion samples using linear attention, a gap of several orders of magnitude.',
    'Work such as Graphormer applies the Transformer directly to structured input and takes the lead on PCQM4Mv2, a large-scale benchmark built for this problem. Seen together, the two technical routes are no longer separate camps; the boundary between them is blurring.',
    'The center of gravity in evaluation is shifting toward closed-loop experiments, where active learning, iterative annotation and uncertainty estimation are folded into the pipeline, and the confidence attached to a model output matters more than any single predicted value.',
  ],
}

export const EN_SCRIPT: MockScriptSegment[] = [
  {
    speaker: 'A',
    text: 'This episode we use one concrete problem to walk through a paper. The setting: you hold a batch of candidates, and before spending money on expensive experiments you want a first estimate of which will meet the requirement and which you can drop. That is the home turf for this line of work.',
  },
  {
    speaker: 'B',
    text: 'So how did people handle this before? My impression is that plenty of teams are still on gradient-boosted trees with hand-crafted features. Where exactly does that fall short, is it accuracy, or is something else going on?',
  },
  {
    speaker: 'A',
    text: 'The early approach compressed every sample into a fixed-length feature vector, which is really just counting local units. The trouble is that it throws away how those units connect and throws away geometry too, so the moment two samples look structurally similar but differ sharply in the property you care about, the model cannot tell them apart.',
  },
  {
    speaker: 'B',
    text: 'So what angle does this paper take instead? Is the point that nobody has to design features by hand anymore, or that the representation itself sits closer to the structure of the data? I would like to know what that shift actually buys you.',
  },
  {
    speaker: 'A',
    text: 'It represents every sample directly as an attributed structure. The units are nodes, the connections are edges, and things like type, degree and whether a node sits on a cycle go in as node features. The connectivity is simply there in the representation, so nobody has to hand-assemble it.',
  },
  {
    speaker: 'B',
    text: 'That sounds like a better representation, but how do you actually get a number out of a structure like that? There has to be a computation in between. Is it passing information from neighbours layer by layer and then pooling it? That is the step whose intuition I have never quite got.',
  },
  {
    speaker: 'A',
    text: 'The core operation is layer-wise aggregation. In each layer, a node first collects messages from its neighbours and sums them, then concatenates that with its own state and pushes the result through a linear transform and a nonlinearity to get its new representation. Stack four or five layers and information reaches four or five connections away.',
  },
  {
    speaker: 'B',
    text: 'Why sum for the aggregation rather than average or max? Is there a real reason behind that choice, or would any of them do? My guess is it has something to do with how many structures the model can tell apart, and I would like you to unpack that.',
  },
  {
    speaker: 'A',
    text: 'There is a real reason. Sum aggregation lines up with the power of the classical structural discriminative test, and swapping in mean or max pooling measurably costs you that ability to discriminate. Summing is also insensitive to node degree, so a node with a lot of connections does not dilute the signal.',
  },
  {
    speaker: 'B',
    text: 'Then is more depth always better? Intuitively deeper means a wider field of view, a larger local structure in sight, which should help on complex input. Is there a visible ceiling in that direction?',
  },
  {
    speaker: 'A',
    text: 'Quite the opposite, and that is a hard constraint for these models. Stack seven or eight layers and you hit over-smoothing: the representations of all nodes converge and the model can no longer see local differences. In practice about five layers is where it saturates, and in the paper going from five layers to eight loses 1.5 points.',
  },
  {
    speaker: 'B',
    text: 'So how do a pile of node representations turn into one overall prediction? There should be another pooling step in there. Would plain averaging over all the nodes be a problem? I want the actual recipe.',
  },
  {
    speaker: 'A',
    text: 'Reducing node representations to a single graph-level vector is what people call readout. Plain averaging gets dominated whenever an oversized sample sits in the same batch, so they use an attention-weighted readout and let the model decide which local structures matter more for the target at hand.',
  },
  {
    speaker: 'B',
    text: 'The architecture is clear now. So what did they do about data efficiency and pretraining? That part sounds just as important, since scarce annotation is a long-standing problem here. How much data did they actually use?',
  },
  {
    speaker: 'A',
    text: 'They run self-supervised pretraining on 11 million unlabeled samples, masking a portion of the node attributes and asking the model to recover them, while doing contrastive learning within the same structure. The benefit shows up most clearly when labels are scarce: with only 500 training labels they come out 8 to 12 points above random initialization.',
  },
  {
    speaker: 'B',
    text: 'And the numbers? I want to know where this actually sits on the public benchmarks, whether it leads by one point or by a wide margin, which is a big difference. Give me the metric names too, so the figures stick.',
  },
  {
    speaker: 'A',
    text: 'On ogbg-molhiv, ROC-AUC goes from the previous best baseline of 0.7558 up to 0.826. On the larger ogbg-molpcba, average precision goes from 0.2266 to 0.291, with more than ninety percent of the 128 subtasks improving. On regression, the average MAE over the QM9 targets drops from 0.045 to 0.031. So on classification you are looking at roughly seven points of ROC-AUC, on the bigger benchmark six and a half points of average precision, and on regression a clear cut in absolute error.',
  },
  {
    speaker: 'B',
    text: 'Then where does it go wrong? You mentioned over-smoothing earlier, what other pitfalls are there? Will this fall apart once it meets a real pipeline? I do not want the good news only, especially not on the data side.',
  },
  {
    speaker: 'A',
    text: 'At least three. First, the expressive ceiling of layer-wise aggregation is set by that classical discriminative power, so geometry-sensitive tasks cannot be solved from two-dimensional structure alone. Second, the pretraining data sits in one distribution and decays noticeably once you move out of it. Third, every metric is an offline benchmark, which is still some distance from the hit rate you would see in a real experiment.',
  },
  {
    speaker: 'B',
    text: 'If a listener wants to go deeper into this, what should they watch next? Which work is worth reading from there, names please, and what is the biggest bottleneck in the field right now?',
  },
  {
    speaker: 'A',
    text: 'Geometrically equivariant networks are the main line: SchNet, DimeNet++ and GemNet write symmetry into the model itself. Pretraining scale keeps growing too, with published work pretraining on 19 million samples and 209 million geometric configurations. And evaluation is moving toward closed-loop experiments, where active learning and uncertainty estimation will matter more and more. If you only follow one thread, follow that last one: the field is shifting from designing architectures to standardizing data and measurement.',
  },
]

/**
 * 英文版论文元信息里**分语言**的那部分：摘要与关键词。
 * 标题 / 作者 / 年份 / 会议 / arXiv 编号两种语言共用（本来就是英文），不在这里。
 */
export const EN_META: Pick<MockPaperMeta, 'abstract' | 'keywords'> = {
  abstract:
    'This work establishes a unified evaluation protocol for molecular property prediction and reruns a range of baselines under the same splits and the same tuning budget. On ogbg-molhiv, ROC-AUC rises from the GIN baseline of 0.7558 to 0.826, and on ogbg-molpcba average precision rises from 0.2266 to 0.291. It proposes a two-channel encoder that fuses two-dimensional topology with three-dimensional conformations; with only 500 training labels, the model runs 8 to 12 points above random initialization.',
  keywords: [
    'Graph Neural Network',
    'Molecular Property Prediction',
    'Message Passing',
    'Self-Supervised Pretraining',
    'OGB',
    'Drug Discovery',
  ],
}
