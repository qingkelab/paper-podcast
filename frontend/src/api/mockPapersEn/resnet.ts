import type { MockAnalysis, MockScriptSegment } from '../mockPapers'
import type { MockPaperMeta } from '../mockPapers'

/** Deep Residual Learning（ResNet）的英文版解读与双人播客脚本（Mock 双语演示用） */
export const EN_ANALYSIS: MockAnalysis = {
  background:
    'Before ResNet, progress in image classification came almost entirely from stacking networks deeper: AlexNet had 8 layers, VGG had 19. But the authors noticed something odd. Take a VGG-style architecture from 20 layers to 56 layers and both training error and test error get worse. That is not overfitting, because the deeper network also does worse on the training set. And it is not simply vanishing gradients either, since BatchNorm plus sensible initialization already lets networks with tens of layers converge. The paper calls this degradation: the extra layers cannot be trained into an identity mapping, so adding depth actively hurts optimization. On ImageNet, a plain 34-layer network has a top-1 error more than 2 points higher than a plain 18-layer one.',
  innovations: [
    'It proposes a residual learning framework: instead of fitting the target mapping directly, the network learns the residual F(x), the difference between input and target. Identity mapping then reduces to driving the residual branch weights close to zero, which an optimizer finds easy.',
    'Residual blocks are built with pure identity shortcuts. The shortcut carries no parameters and does no multiplication, only element-wise addition, so going deeper adds almost no parameters and almost no compute.',
    'It handles dimension mismatch two ways: an identity shortcut combined with a 1×1 convolution projection, or zero-padding to fill in the extra channels. Ablations show the projection version is slightly better, but the gap is small.',
    'It designs the bottleneck block: above 50 layers, the two 3×3 convolutions become 1×1 reduce, 3×3, 1×1 expand, keeping the 152-layer network under 11.3 billion multiply-adds, lower than VGG-19.',
    'It transfers ImageNet-pretrained deep features straight to detection: swapping in ResNet as the Faster R-CNN backbone lifts COCO mAP by 6 points absolute, confirming that residual representations are transferable.',
  ],
  method:
    "The residual block is written h(x) = ReLU(x + F(x)), where F is usually two 3×3 convolutions. If the best transform for the block is close to identity, the optimizer only has to push the last convolution's weights in F toward zero, which is far easier than asking a stack of nonlinear layers to fit identity on its own. The shortcut is a pure identity mapping, with no gates and no extra parameters: in the forward pass it adds a path that skips the nonlinearity, and in the backward pass it leaves the gradient a route back that barely attenuates. One detail is easy to overlook: the activation comes after the addition, so it sits on the residual path and the identity path itself is never broken. The overall architecture follows VGG's stacked small kernels, doubling channels and halving spatial size at each downsampling step, and ends with a single global average pooling layer plus a 1000-way fully connected layer, with fewer than one tenth the parameters of VGG-16.",
  experiments:
    'On ImageNet, ResNet-34 reaches 3.76% top-5 error, while ResNet-152 reaches 4.49% as a single model and 3.57% as an ensemble, winning the ILSVRC 2015 classification track. On CIFAR-10, residuals let a 110-layer network hit 6.43% error, while the 1202-layer version overfits and climbs back to 7.93%. For detection, swapping the Faster R-CNN backbone to ResNet-101 takes COCO mAP from 21.2 to 27.2. Training uses SGD with momentum 0.9 and an initial learning rate of 0.1.',
  conclusion:
    'The core conclusion is that depth is not the root of the problem; whether identity mapping can be learned is. Residual connections turn the extra layers into an optional fine-tune: train them well and they contribute new features, train them badly and they collapse back into an identity path, so the network is at worst no worse than its shallower counterpart. The change is small enough to be a single addition, yet it makes networks above 100 layers stably trainable for the first time, at lower compute than the shallow large models of the same era.',
  limitations: [
    'The explanation of degradation stays at the level of intuition. The paper never gives a theoretical account of why deep networks struggle to fit identity mappings; the more formal discussion was added by later work.',
    'Residual connections do not solve every deep-network problem. On CIFAR-10 the 1202-layer network actually gets worse, rising to 7.93% error, which shows that extreme depth still overfits on small datasets.',
    'The paper reports no measured inference latency, memory footprint, or edge-deployment numbers, yet those are exactly the constraints a 152-layer model hits first in industry.',
    'The transfer evaluation covers only two or three tasks in detection and segmentation, with no cross-modal validation, and it does not address the instability of BatchNorm batch statistics when batches are small.',
  ],
  value:
    'ResNet is the most reused backbone in computer vision. In detection, Faster R-CNN and Mask R-CNN; in segmentation, FCN and DeepLab; their backbones were later largely swapped for ResNet or one of its variants. In industrial inspection and medical imaging, where labeled data is scarce, people almost always fine-tune an ImageNet-pretrained ResNet. It turned training a deeper network from an adventure into the default option.',
  future: [
    'Improvements around the residual block itself followed quickly: ResNeXt introduced grouped convolutions, DenseNet switched to dense connections, and Wide ResNet chose to widen channels instead of going deeper.',
    'Follow-up work in 2016 reordered BatchNorm and initialized the end of the residual branch to zero, pushing a 1001-layer network to 4.62% error on CIFAR; the change is called pre-activation.',
    "Detection and segmentation moved on through FPN and Mask R-CNN, where ResNet's multi-level features became a natural carrier for feature pyramids, and later gave rise to deformable convolutions and attention-augmented backbones.",
    'By the Transformer era, ViT and Swin still write residual plus layer normalization; residual connections are now the default precondition for training deep networks, and almost nobody tries a deep network without them.',
  ],
}

export const EN_SCRIPT: MockScriptSegment[] = [
  {
    speaker: 'A',
    text: 'This one is Deep Residual Learning for Image Recognition, arXiv 1512.03385, posted in late 2015 by Kaiming He, Xiangyu Zhang, Shaoqing Ren and Jian Sun. It came out at CVPR 2016 and won the ILSVRC 2015 image classification track.',
  },
  {
    speaker: 'B',
    text: 'I remember 2015 ImageNet was already a crowded race, with VGG sitting at 19 layers. So is the pitch here just more depth? 152 layers sounds more like a contest of who is willing to burn compute than genuine methodological novelty.',
  },
  {
    speaker: 'A',
    text: 'Depth is only the surface. What they set out to explain is an anomaly. Take a VGG-style architecture from 20 layers and stack it up to 56, and training error and test error both get worse. That is not overfitting, because the deeper network also performs worse on the training set. They call the phenomenon degradation.',
  },
  {
    speaker: 'B',
    text: 'Hold on, that does not match intuition. More layers should mean more capacity. Even if it learns nothing new, the extra layers could at least learn to pass the input through unchanged, so how does that end up dragging the whole network down?',
  },
  {
    speaker: 'A',
    text: "That is exactly the paper's key argument. In theory, extra layers doing identity mapping could not be worse than the shallower network, but getting a stack of nonlinear layers to fit identity is very hard. So they changed the setup: rather than have the network fit the target mapping directly, let it learn the residual.",
  },
  {
    speaker: 'B',
    text: 'How is that actually implemented? Is this the skip connection everyone talks about? And I still want to ask: once a convolution changes the channel count and the spatial size, can you really just add them straight through?',
  },
  {
    speaker: 'A',
    text: 'It is written y = F(x) + x, where x is the input and F is what two or three convolution layers learn, and that shortcut carries no extra parameters at all. If identity mapping is optimal, the network only has to push the weights of F close to zero, which is far easier than fitting identity the other way around. When dimensions do not match, a 1×1 convolution projects them into alignment. The paper compares the two, and the projection version is slightly better, but the gap is small.',
  },
  {
    speaker: 'B',
    text: 'Are there concrete numbers for degradation? I want to know how obvious it is, whether it is worth a whole paper, or just a few points of normal fluctuation. And they ran controlled experiments on CIFAR-10 as well, right?',
  },
  {
    speaker: 'A',
    text: 'On ImageNet, a plain 34-layer network has a top-1 error more than 2 points higher than a plain 18-layer one, while with residuals the 34-layer version beats the 18-layer by roughly 3 points. CIFAR-10 is starker: plain 56 layers is worse than 20, but the residual version reaches 6.43% error at 110 layers, and more layers keeps helping.',
  },
  {
    speaker: 'B',
    text: 'And the final numbers? What did ILSVRC look like that year, and how much did the compute go up? If this is just buying accuracy with hardware, the paper gets a lot less persuasive, and that is the part I care about.',
  },
  {
    speaker: 'A',
    text: "The 152-layer single model lands at 4.49% top-5 error and 3.57% as an ensemble, the only entry that year to push top-5 below 3.6%. The interesting part is that this 152-layer network needs only 11.3 billion multiply-adds, lower than VGG-19's 19.6 billion. Eight times deeper, and cheaper to run.",
  },
  {
    speaker: 'B',
    text: 'I am a bit skeptical about that. Is it because VGG stacked channels and resolution too aggressively, or does the bottleneck structure really save that much compute? From those two numbers alone, I do not think we can conclude anything.',
  },
  {
    speaker: 'A',
    text: 'Mostly the first one. VGG piles on channels and resolution: 138 million parameters, and a single training run took weeks back then. ResNet-34, by comparison, needs only 3.6 billion multiply-adds, about 18% of VGG-19. So after residuals, depth stopped being a compute burden and became an efficiency tool.',
  },
  {
    speaker: 'B',
    text: 'Did they do anything beyond classification? I remember this paper also has a detection section. Running the same backbone on detection would say more about the value of residual features themselves, would it not?',
  },
  {
    speaker: 'A',
    text: 'They did, and it may be the most convincing step in the paper. Swap the Faster R-CNN backbone from VGG-16 to ResNet-101 and COCO mAP goes from 21.2 to 27.2, a 6-point absolute gain, close to thirty percent relative. The representations learned by residuals transfer to other tasks.',
  },
  {
    speaker: 'B',
    text: 'What about the training details? I want to know how they kept a 152-layer network stable. At that depth it sounds like it would fall apart halfway through, or diverge the moment the learning rate creeps up.',
  },
  {
    speaker: 'A',
    text: 'The key is BatchNorm, applied after every convolution, and that is the precondition for going this deep. The optimizer is SGD with momentum 0.9, weight decay 0.0001, batch size 256, learning rate starting at 0.1 and divided by 10 when the error plateaus, for roughly 600,000 iterations.',
  },
  {
    speaker: 'B',
    text: 'So where do you think the limitations are? With this many citations it has basically become the standard answer. If you had to nitpick, which would you raise first: the theory not being solid enough, or extreme depth having a ceiling of its own?',
  },
  {
    speaker: 'A',
    text: 'First, degradation only has an intuitive argument, with no theoretical characterization of why deep networks struggle to fit identity. Second, the 1202-layer CIFAR network goes back up to 7.93% error, so extreme depth still overfits on small data. Third, the paper never reports inference latency or memory footprint, and those are precisely the first constraints a 152-layer model hits in deployment.',
  },
  {
    speaker: 'B',
    text: 'But its impact is undeniable, nobody disputes that. Can you name a few specific follow-ups, so listeners know where to read next? Ideally the ones people are still using today.',
  },
  {
    speaker: 'A',
    text: 'The most direct are DenseNet and ResNeXt, one switched to dense connections and the other introduced grouped convolutions. In detection and segmentation, the backbones of FPN and Mask R-CNN are basically all ResNet. And the 2016 improved version reordered the normalization and zero-initialized the end of the residual branch, training a 1001-layer network down to 4.62% error on CIFAR.',
  },
]

/**
 * 英文版论文元信息里**分语言**的那部分：摘要与关键词。
 * 标题 / 作者 / 年份 / 会议 / arXiv 编号两种语言共用（本来就是英文），不在这里。
 */
export const EN_META: Pick<MockPaperMeta, 'abstract' | 'keywords'> = {
  abstract:
    'The paper proposes a residual learning framework in which identity shortcut connections let a deep network learn the residual between the input and the target. On ImageNet, ResNet-152 reaches 4.49% top-5 error as a single model and 3.57% as an ensemble, winning the ILSVRC 2015 classification track; this 152-layer network needs only 11.3 billion multiply-adds, lower than the 19.6 billion of VGG-19. On CIFAR-10, residuals let a 110-layer network reach 6.43% error, and more layers keep helping.',
  keywords: [
    'ResNet',
    'Residual Learning',
    'Image Classification',
    'Batch Normalization',
    'Object Detection',
    'Degradation of Deep Networks',
  ],
}
