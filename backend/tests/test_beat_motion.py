"""逐句画面动效的**渲染与编码那一侧**（设计见 `docs/BEAT_MOTION.md`）。

这一组只测「给定的逐句图层有没有真的画进 MP4」：逐句图片卡在片段内部溶解、
数字强调层紧跟它自己那一句的字幕带、以及三条守卫（同图 / 太短 / 封面与片尾）。
判据尽量落在**像素**上 —— 滤镜字符串对了但画面没动，是这块最容易出现的假绿。

`Scene(beats=[])` 走老路径这条单独钉着：不给新参数时编出来的 MP4 应当**逐字节相同**
（编码是确定性的，同样输入必然同样字节）。
"""

from __future__ import annotations

import hashlib
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.design import (
    IMAGE_BOX_H,
    IMAGE_BOX_LEFT,
    IMAGE_TOP,
    NUMBER_BG,
    POINT_BG,
    PORTRAIT,
    SUBTITLE_TOP,
    VIDEO_H,
    VIDEO_W,
)
from app.services.htmlframe import ChromeSession, chrome_available
from app.services.video_core import Beat, CaptionBand, Scene
from app.services.video_encode import encode_video, ffmpeg_available, probe_media_duration
from app.services.video_render import (
    render_beat_layers,
    render_caption_band,
    render_caption_chip,
    render_chrome,
    render_image_card,
)

requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="需要系统安装 ffmpeg")
requires_chrome = pytest.mark.skipif(
    not chrome_available(), reason="这台机器上没有 Chrome，跳过需要真渲染的用例"
)

IMAGE_CENTER = (VIDEO_W // 2, IMAGE_TOP + IMAGE_BOX_H // 2)


@dataclass
class FakeTiming:
    """`compose_video` 只用到 start / end / speaker（`build_scenes` 的时间轴）。"""

    index: int
    speaker: str
    start: float
    end: float


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def make_png(path: Path, w: int = 700, h: int = 500, color=(30, 60, 100)) -> Path:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    pix.save(str(path))
    return path


def make_audio(path: Path, seconds: float, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def extract_frame(video: Path, at: float, output: Path) -> Path:
    """抽一帧。`-ss` 必须放在 `-i` **之后** —— 放前面时邻近时刻会抽到同一帧。"""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", str(video), "-ss", f"{at:.3f}", "-frames:v", "1", str(output),
        ],
        capture_output=True, text=True, check=True,
    )
    return output


def frame_pixels(path: Path):
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    n, width, samples = pix.n, pix.width, pix.samples

    def at(x: int, y: int) -> tuple[int, int, int]:
        i = (y * width + x) * n
        return (samples[i], samples[i + 1], samples[i + 2])

    return at


def count_frames(video: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames",
            "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
            "-of", "csv=p=0", str(video),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return int(out)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capsule_bbox(path: Path) -> tuple[int, int, int, int] | None:
    """数字胶囊（`NUMBER_BG`）的包围盒。带底 `SUBTITLE_BG` 与它差 24.8 个亮度级
    （旧值只差 3.4，那时容差一放宽整条带子都会被算成胶囊），这里仍保守取 3。"""
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    n, width, height, samples = pix.n, pix.width, pix.height, pix.samples
    target = tuple(int(NUMBER_BG[i : i + 2], 16) for i in (1, 3, 5))
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        row = y * width * n
        for x in range(width):
            i = row + x * n
            if (
                abs(samples[i] - target[0]) <= 3
                and abs(samples[i + 1] - target[1]) <= 3
                and abs(samples[i + 2] - target[2]) <= 3
            ):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------
# 渲染侧的守卫（不需要 ffmpeg）
# --------------------------------------------------------------------------


class TestBeatCardGuards:
    """三条守卫必须在**渲染**这一步就挡住，避免白渲染一堆用不上的图层。"""

    def _scene(self, tmp_path: Path, **kwargs) -> tuple[Scene, Path, Path]:
        red = make_png(tmp_path / "red.png", color=(220, 30, 30))
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        scene = Scene(start=0, end=6.0, image=red, kind="figure", **kwargs)
        return scene, red, blue

    def test_short_beat_keeps_the_previous_card(self, tmp_path):
        """太短的那句不切（按**实际窗口**判，不是按字数估）。"""
        beats = [Beat(text="这一句很长，长到独占这一段的绝大部分时间。" * 3)]
        beats.append(Beat(text="短句。", image=str(make_png(tmp_path / "blue.png"))))
        scene = Scene(start=0, end=6.0, image=make_png(tmp_path / "red.png"), kind="figure", beats=beats)

        layers = render_beat_layers(scene, work_dir=tmp_path / "layers")

        # 3 字 vs 60 多字 → 这一句只有零点几秒，远低于 BEAT_IMAGE_MIN_SEC
        assert layers.cards == [None, None]

    def test_same_image_as_the_previous_beat_is_not_re_laid(self, tmp_path):
        """和上一句同图不切：图没变就别动，无意义的溶解只会让人以为卡了一下。"""
        red = make_png(tmp_path / "red.png", color=(220, 30, 30))
        scene = Scene(
            start=0,
            end=6.0,
            image=red,
            kind="figure",
            beats=[
                Beat(text="第一句讲的是一件事，够长够长够长。"),
                Beat(text="第二句还在讲同一张图，也够长够长。", image=str(red)),
            ],
        )

        layers = render_beat_layers(scene, work_dir=tmp_path / "layers")

        assert layers.cards == [None, None]

    def test_cover_and_outro_never_swap_the_card(self, tmp_path):
        """封面段与片尾品牌段一律不渲染逐句卡（「第一帧必须是论文首页」是产品要求）。"""
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        for extra in ({"headline": "爆款标题"}, {"brand": "outro"}):
            scene = Scene(
                start=0,
                end=6.0,
                image=make_png(tmp_path / "cover.png", color=(250, 250, 250)),
                kind="cover",
                beats=[
                    Beat(text="第一句够长够长够长够长够长。"),
                    Beat(text="第二句也够长够长够长够长。", image=str(blue)),
                ],
                **extra,
            )
            layers = render_beat_layers(scene, work_dir=tmp_path / f"layers-{sorted(extra)[0]}")
            assert layers.cards == [None, None], extra

    def test_beats_empty_renders_no_layer_at_all(self, tmp_path):
        """`Scene(beats=[])`（= 老数据）不产出任何逐句图层。"""
        scene = Scene(
            start=0, end=3.0, image=make_png(tmp_path / "img.png"), kind="figure", text="一段话。"
        )
        layers = render_beat_layers(scene, work_dir=tmp_path / "layers")
        assert layers.cards == [] and layers.chips == []

    def test_the_second_beat_does_get_its_own_card(self, tmp_path):
        """正例：换图 + 时长够 → 第 1 句有卡，第 0 句没有（它就是本段原本的卡）。"""
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        scene = Scene(
            start=0,
            end=6.0,
            image=make_png(tmp_path / "red.png", color=(220, 30, 30)),
            kind="figure",
            beats=[
                Beat(text="第一句讲的是这件事的来龙去脉，先说清楚。"),
                Beat(text="第二句换了一张图，讲的是另一件事。", image=str(blue)),
            ],
        )

        layers = render_beat_layers(scene, work_dir=tmp_path / "layers")

        assert layers.cards[0] is None
        assert layers.cards[1] is not None and layers.cards[1].exists()


# --------------------------------------------------------------------------
# 真编码：逐句换画面 + 长度不变
# --------------------------------------------------------------------------


@requires_ffmpeg
class TestBeatCardsAreEncoded:
    FIRST = "第一句话讲的是这件事的来龙去脉。"
    SECOND = "第二句话换了另外一张图来讲。"
    SECONDS = 6.0

    def _encode(self, tmp_path: Path, *, beat_cards, cover: bool = False, chips=None) -> Path:
        red = make_png(tmp_path / "red.png", color=(220, 30, 30))
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        scene = Scene(
            start=0,
            end=self.SECONDS,
            image=red,
            kind="cover" if cover else "figure",
            caption="Figure 1: 示意图",
            headline="爆款标题" if cover else "",
        )
        chrome = render_chrome(scene, tmp_path / "chrome.png")
        card = render_image_card(scene, tmp_path / "card.png")
        bands = [
            CaptionBand(self.FIRST, render_caption_band(self.FIRST, tmp_path / "band0.png")),
            CaptionBand(self.SECOND, render_caption_band(self.SECOND, tmp_path / "band1.png")),
        ]
        audio = make_audio(tmp_path / "audio.wav", self.SECONDS)
        out = tmp_path / "out.mp4"
        encode_video(
            [scene], [chrome], audio, out,
            caption_bands=[bands],
            image_cards=[card],
            beat_cards=[beat_cards],
            beat_chips=[chips] if chips else None,
            waveform=False,
        )
        return out

    def test_the_second_beat_shows_its_own_picture(self, tmp_path):
        """一句一图：抽两句中心的帧，第一句是红、第二句是蓝。"""
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        scene = Scene(
            start=0, end=self.SECONDS, image=make_png(tmp_path / "red.png", color=(220, 30, 30)),
            kind="figure",
            beats=[Beat(text=self.FIRST), Beat(text=self.SECOND, image=str(blue))],
        )
        layers = render_beat_layers(scene, work_dir=tmp_path / "layers")
        assert layers.cards[1] is not None

        video = self._encode(tmp_path, beat_cards=layers.cards)
        # 两句按字数分 6 秒：第一句 [0, 2.9)、第二句 [2.9, 6.0)（溶解 0.25 秒）
        first = frame_pixels(extract_frame(video, 1.4, tmp_path / "f1.png"))(*IMAGE_CENTER)
        second = frame_pixels(extract_frame(video, 4.5, tmp_path / "f2.png"))(*IMAGE_CENTER)

        assert first[0] > 150 and first[2] < 100, f"第一句应是红图：{first}"
        assert second[2] > 150 and second[0] < 100, f"第二句应是蓝图：{second}"

    def test_frame_count_and_duration_do_not_move(self, tmp_path):
        """所有新图层都在**片段内部**，所以总帧数一个都不许变。"""
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        scene = Scene(
            start=0, end=self.SECONDS, image=make_png(tmp_path / "red.png", color=(220, 30, 30)),
            kind="figure",
            beats=[Beat(text=self.FIRST), Beat(text=self.SECOND, image=str(blue))],
        )
        layers = render_beat_layers(scene, work_dir=tmp_path / "layers")
        with_beats = self._encode(tmp_path, beat_cards=layers.cards)
        without = self._encode(tmp_path, beat_cards=None)

        assert count_frames(with_beats) == count_frames(without) == int(self.SECONDS * 30)
        assert probe_media_duration(with_beats) == pytest.approx(
            probe_media_duration(without), abs=0.02
        )
        assert probe_media_duration(with_beats) == pytest.approx(self.SECONDS, abs=0.3)

    def test_cover_scene_keeps_the_cover_throughout(self, tmp_path):
        """封面段即使被硬塞了两张不同的卡，画面也不许换。"""
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        fake = render_image_card(
            Scene(start=0, end=1.0, image=blue, kind="cover", headline=""), tmp_path / "fake-blue.png"
        )

        video = self._encode(tmp_path, beat_cards=[None, fake], cover=True)

        first = frame_pixels(extract_frame(video, 1.4, tmp_path / "c1.png"))(*IMAGE_CENTER)
        second = frame_pixels(extract_frame(video, 4.5, tmp_path / "c2.png"))(*IMAGE_CENTER)
        assert first == second, f"封面段不该换画面：{first} → {second}"
        # 而且仍然是封面的毛玻璃页（暖调），不是那张被硬塞进来的蓝图（蓝调）
        assert second[0] >= second[2] and second[2] < second[0] + 5, f"第二句不该变成蓝图：{second}"


@requires_ffmpeg
class TestOldPathDoesNotChange:
    """`beats=[]` 必须走和改动前**完全一样**的路径：同输入 → 同一份字节。"""

    def _encode(self, tmp_path: Path, out_name: str, **kwargs) -> Path:
        image = make_png(tmp_path / "img.png", color=(200, 90, 40))
        scene = Scene(start=0, end=4.0, image=image, kind="figure", text="整整一段就是一句话。")
        chrome = render_chrome(scene, tmp_path / "chrome.png")
        band = render_caption_band("整整一段就是一句话。", tmp_path / "band.png")
        audio = make_audio(tmp_path / "audio.wav", 4.0)
        out = tmp_path / out_name
        encode_video(
            [scene], [chrome], audio, out,
            caption_bands=[[CaptionBand("整整一段就是一句话。", band)]],
            image_cards=[render_image_card(scene, tmp_path / "card.png")],
            waveform=False,
            **kwargs,
        )
        return out

    def test_empty_beat_lists_are_byte_identical_to_the_old_call(self, tmp_path):
        old = self._encode(tmp_path, "old.mp4")
        new = self._encode(tmp_path, "new.mp4", beat_cards=[[]], beat_chips=[[]])

        assert sha256(old) == sha256(new)
        assert count_frames(new) == int(4.0 * 30)


# --------------------------------------------------------------------------
# 数字强调层
# --------------------------------------------------------------------------


class TestCaptionChipLayer:
    SENTENCE = "准确率从 61% 提到了 67%。"
    PLAIN = "这句话里没有任何阿拉伯数字。"

    def test_no_numbers_means_no_layer(self, tmp_path):
        assert render_caption_chip(self.PLAIN, tmp_path / "none.png") is None
        assert not (tmp_path / "none.png").exists()

    def test_the_chip_lands_on_the_bands_own_capsule(self, tmp_path):
        """胶囊层的位置必须和整条字幕带里那颗胶囊对齐（这正是「单独出一层」的前提）。"""
        band = render_caption_band(self.SENTENCE, tmp_path / "band.png")
        chip = render_caption_chip(self.SENTENCE, tmp_path / "chip.png")
        assert chip is not None

        band_box = capsule_bbox(band)
        chip_box = capsule_bbox(chip)
        assert band_box is not None and chip_box is not None
        # 同一行、同一基线：两层的胶囊包围盒最多差 2px
        assert max(abs(a - b) for a, b in zip(band_box, chip_box)) <= 2, (band_box, chip_box)

    def test_the_chip_layer_is_transparent_outside_the_capsule(self, tmp_path):
        """除了胶囊，其余必须透明 —— 不透明的话整条字幕带会被它盖成一块白。"""
        import pymupdf

        chip = render_caption_chip(self.SENTENCE, tmp_path / "chip.png")
        pix = pymupdf.Pixmap(str(chip))
        assert pix.alpha, "chip 层必须是带透明通道的 PNG"
        # 左上角（远离胶囊）应当完全透明
        i = (5 * pix.width + 5) * pix.n
        assert pix.samples[i + 3] == 0, "胶囊以外还有不透明像素"

    @requires_chrome
    def test_html_path_alignment(self, tmp_path, monkeypatch):
        """HTML 渲染路径（线上默认）同样对齐：两个页面共用同一份 CSS 与 DOM。"""
        from app.services.htmlpage import caption_band_page, caption_chip_page

        with ChromeSession.start() as session:
            band = session.render(
                caption_band_page(self.SENTENCE, PORTRAIT), tmp_path / "html-band.png",
                work_dir=tmp_path,
            )
            chip = session.render(
                caption_chip_page(self.SENTENCE, PORTRAIT), tmp_path / "html-chip.png",
                work_dir=tmp_path,
            )

        band_box = capsule_bbox(band)
        chip_box = capsule_bbox(chip)
        assert band_box is not None and chip_box is not None
        assert max(abs(a - b) for a, b in zip(band_box, chip_box)) <= 2, (band_box, chip_box)

    @requires_ffmpeg
    def test_the_chip_layer_is_encoded_after_its_own_band(self, tmp_path):
        """含数字的句子带上 chip 层编码后，字幕带位置仍然有那一颗胶囊（没被冲掉）。

        这一条测的是 `encode_video` 单独一层（带子自己画着胶囊 + chip 叠上去）——
        所以两帧都数得到胶囊。**整条链**（`compose_video`）不是这么走的：
        带子按 `chip=False` 渲染，胶囊只由 chip 层弹出来（见 `TestWholeChainWithBeats`）。
        """
        first, second = self.SENTENCE, "第二句话里只有一个数字 3。"
        image = make_png(tmp_path / "img.png", color=(230, 235, 240))
        scene = Scene(start=0, end=6.0, image=image, kind="figure")
        chrome = render_chrome(scene, tmp_path / "chrome.png")
        card = render_image_card(scene, tmp_path / "card.png")
        bands = [
            CaptionBand(first, render_caption_band(first, tmp_path / "b0.png")),
            CaptionBand(second, render_caption_band(second, tmp_path / "b1.png")),
        ]
        chips = [
            render_caption_chip(first, tmp_path / "c0.png"),
            render_caption_chip(second, tmp_path / "c1.png"),
        ]
        assert chips[0] is not None and chips[1] is not None

        out = tmp_path / "chip.mp4"
        encode_video(
            [scene], [chrome], make_audio(tmp_path / "a.wav", 6.0), out,
            caption_bands=[bands], image_cards=[card], beat_chips=[chips], waveform=False,
        )

        # 第二句的起点大约在 3.0 秒（按字数 13 vs 12 分 6 秒），chip 在 +0.12 秒弹出
        before = extract_frame(out, 3.05, tmp_path / "p0.png")
        after = extract_frame(out, 3.9, tmp_path / "p1.png")
        region = (0, SUBTITLE_TOP + 4, VIDEO_W, VIDEO_H - 4)

        def capsule_count(path: Path) -> int:
            import pymupdf

            pix = pymupdf.Pixmap(str(path))
            n, width, samples = pix.n, pix.width, pix.samples
            target = tuple(int(NUMBER_BG[i : i + 2], 16) for i in (1, 3, 5))
            total = 0
            for y in range(region[1], region[3]):
                for x in range(region[0], region[2]):
                    i = (y * width + x) * n
                    if (
                        abs(samples[i] - target[0]) <= 10
                        and abs(samples[i + 1] - target[1]) <= 10
                        and abs(samples[i + 2] - target[2]) <= 10
                    ):
                        total += 1
            return total

        # 编码有量化噪声，用容差 10 数「强调色像素」——两帧都该有那颗胶囊
        assert capsule_count(before) > 0
        assert capsule_count(after) > 0


# --------------------------------------------------------------------------
# 版式决定：这一句有强调层时，字幕带**不画**那颗胶囊（见 docs/BEAT_MOTION.md）
#
# 不画的原因很实在：带子里本来就画了胶囊的话，chip 层再叠一次 = 视觉上什么都没变
# （实测两帧的强调色像素数一样）。数字仍然出现在**同一处**（只改颜色、不动 padding /
# 字号），所以「句子先出现 → 0.12 秒后那颗数字被点出来」才是真的看得见的动效。
# --------------------------------------------------------------------------


class TestBandCanLeaveTheCapsuleToTheChipLayer:
    SENTENCE = "准确率从 61% 提到了 67%。"

    def test_default_keeps_the_capsule(self, tmp_path):
        """默认 `chip=True`：带子自己画胶囊 —— 老观感一个字都不许变。"""
        assert capsule_bbox(render_caption_band(self.SENTENCE, tmp_path / "on.png")) is not None

    def test_chip_false_leaves_the_capsule_to_the_chip_layer(self, tmp_path):
        """`chip=False`：带子里没有胶囊，数字仍然在（同一处、普通文字颜色）。"""
        band = render_caption_band(self.SENTENCE, tmp_path / "off.png", chip=False)
        assert capsule_bbox(band) is None, "chip=False 的带子里不该有胶囊底色"

        # 数字还在：把它和 chip 层的胶囊 bbox 对齐着看，墨迹（暗像素）落在同一块里
        chip = render_caption_chip(self.SENTENCE, tmp_path / "chip.png")
        chip_box = capsule_bbox(chip)
        assert chip_box is not None
        import pymupdf

        pix = pymupdf.Pixmap(str(band))
        n, width, samples = pix.n, pix.width, pix.samples
        dark = 0
        for y in range(chip_box[1], chip_box[3] + 1):
            for x in range(chip_box[0], chip_box[2] + 1):
                i = (y * width + x) * n
                if samples[i] < 120 and samples[i + 1] < 120 and samples[i + 2] < 120:
                    dark += 1
        assert dark > 100, f"数字应当照常画出来（只是没有底色）：{dark}"

    @requires_chrome
    def test_html_path_also_drops_the_capsule(self, tmp_path):
        """HTML 渲染路径（线上默认）同样：`chip=False` 时那颗胶囊整个不画。"""
        from app.services.htmlpage import caption_band_page

        with ChromeSession.start() as session:
            on = session.render(
                caption_band_page(self.SENTENCE, PORTRAIT), tmp_path / "on.png", work_dir=tmp_path
            )
            off = session.render(
                caption_band_page(self.SENTENCE, PORTRAIT, chip=False),
                tmp_path / "off.png",
                work_dir=tmp_path,
            )

        assert capsule_bbox(on) is not None
        assert capsule_bbox(off) is None
        # 位置不能动：两版的胶囊 bbox 只在「有底色」时存在，所以改成比字的位置 ——
        # 同一句话两版渲染出来的**墨迹**外框必须一致（只换了颜色）
        assert _ink_bbox(on) == _ink_bbox(off), (_ink_bbox(on), _ink_bbox(off))


def _ink_bbox(path: Path) -> tuple[int, int, int, int] | None:
    """整层里「深色墨迹」的包围盒（数字和文字都用深色画，胶囊底色不算）。"""
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    n, width, height, samples = pix.n, pix.width, pix.height, pix.samples
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        row = y * width * n
        for x in range(width):
            i = row + x * n
            if samples[i] < 120 and samples[i + 1] < 120 and samples[i + 2] < 120:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def count_capsule_pixels(path: Path, region: tuple[int, int, int, int]) -> int:
    """数一帧里**胶囊底色**（`NUMBER_BG`）的像素。

    容差取 **3**：底色 `SUBTITLE_BG`(#f4f7fa) 与胶囊底相差 24.8 个亮度级，
    容差放到 10 不会再把整条带子数进去 —— 但**旧值 #eef4fb 只差 3.4**，
    当年正是「容差一放大就两帧一样」把「叠上去看不出来」那个 bug 藏住了。
    """
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    n, width, samples = pix.n, pix.width, pix.samples
    target = tuple(int(NUMBER_BG[i : i + 2], 16) for i in (1, 3, 5))
    total = 0
    for y in range(region[1], region[3]):
        for x in range(region[0], region[2]):
            i = (y * width + x) * n
            if all(abs(samples[i + k] - target[k]) <= 3 for k in range(3)):
                total += 1
    return total


# --------------------------------------------------------------------------
# 端到端：走 `compose_video`（这是「接线真的通了」的唯一证据）
#
# 上面那些用例都是**直接调 encode_video**，接线断了照样能过 —— 真正要证明的是
# 「渲染出来的逐句卡 / 强调层真的被编码用了」。所以这里从 `compose_video` 走完整链：
# 一段脚本两句、两张纯色卡、真跑 ffmpeg，再从成片里抽帧取像素。
# --------------------------------------------------------------------------


@requires_ffmpeg
class TestWholeChainWithBeats:
    FIRST = "第一句话讲的是这件事的来龙去脉。"
    SECOND = "第二句话换了另外一张图来讲。"
    SECONDS = 6.0

    def _compose(
        self,
        tmp_path: Path,
        *,
        name: str,
        beats: list[dict] | None,
        second: str | None = None,
        record: dict | None = None,
    ):
        """一段两句的完整合成。`beats=None` = 不给逐句画面（= 老数据的形状）。

        用 `preset_scenes` 喂画面分配：那是**重新合成**那条路（不调模型），
        所以这里一个模型调用都不会发生，`Scene.beats` 却和线上一样是真的。
        """
        from app.services.video import compose_video

        first = self.FIRST
        second = second or self.SECOND
        red = make_png(tmp_path / "red.png", color=(220, 30, 30))
        blue = make_png(tmp_path / "blue.png", color=(30, 60, 220))
        audio = make_audio(tmp_path / "audio.wav", self.SECONDS)

        stored: dict = {"index": 0, "image": "f1", "point": "", "focus": None, "why": ""}
        if beats is not None:
            stored["beats"] = beats

        # 把 `encode_video` 的真实入参记下来：**这是接线的直接证据**（渲染出来的逐句图层
        # 到底有没有交到编码侧）。只包一层，不改它的行为。
        import app.services.video as video_module

        real_encode = video_module.encode_video

        def spy(*args, **kwargs):
            if record is not None:
                record.update(kwargs)
            return real_encode(*args, **kwargs)

        video_module.encode_video = spy
        try:
            result = compose_video(
                segments=[{"speaker": "A", "text": first + second}],
                timings=[FakeTiming(index=0, speaker="A", start=0.0, end=self.SECONDS)],
                audio_path=audio,
                audio_duration=self.SECONDS,
                cover_path=None,  # 没有封面段 → 逐句卡这条路才是通的（封面段是硬守卫）
                figures=[
                    {"id": "f1", "path": str(red), "caption": "红图"},
                    {"id": "f2", "path": str(blue), "caption": "蓝图"},
                ],
                illustration_png=None,
                title="",
                llm=None,  # 复用模式本来就不该碰模型
                work_dir=tmp_path / f"work-{name}",
                output_path=tmp_path / f"{name}.mp4",
                preset_scenes=[stored],
                preset_assets={"f1": str(red), "f2": str(blue)},
            )
        finally:
            video_module.encode_video = real_encode
        return result, tmp_path / f"{name}.mp4", tmp_path / f"work-{name}"

    def test_two_beats_two_colours_through_the_whole_chain(self, tmp_path):
        """一句一图：抽两句中心的帧，第一句红、第二句蓝；时长/帧数与不给 beats 时一致。"""
        beats = [
            {"text": self.FIRST, "image": "f1"},
            {"text": self.SECOND, "image": "f2"},
        ]
        result, video, _ = self._compose(tmp_path, name="with", beats=beats)
        _, plain, _ = self._compose(tmp_path, name="without", beats=None)

        assert [item["image"] for item in result.scenes[0]["beats"]] == ["", "f2"]

        # 两句按字数分 6 秒：第一句 [0, 2.909)、第二句 [2.909, 6.0)（溶解 0.25 秒）
        first = frame_pixels(extract_frame(video, 1.4, tmp_path / "f1.png"))(*IMAGE_CENTER)
        second = frame_pixels(extract_frame(video, 4.5, tmp_path / "f2.png"))(*IMAGE_CENTER)
        assert first[0] > 150 and first[2] < 100, f"第一句应是红图：{first}"
        assert second[2] > 150 and second[0] < 100, f"第二句应是蓝图：{second}"

        # 不给 beats → 老路径：整段都是第一张卡（这一条同时证明上面的差别来自接线）
        untouched = frame_pixels(extract_frame(plain, 4.5, tmp_path / "f3.png"))(*IMAGE_CENTER)
        assert untouched[0] > 150 and untouched[2] < 100, f"不给 beats 时不该换画面：{untouched}"

        # 所有新图层都在**片段内部**：总帧数与总时长一个都不许变
        assert count_frames(video) == count_frames(plain) == int(self.SECONDS * 30)
        assert probe_media_duration(video) == pytest.approx(
            probe_media_duration(plain), abs=0.02
        )
        assert probe_media_duration(video) == pytest.approx(self.SECONDS, abs=0.3)

    def test_the_digit_pops_out_only_after_its_own_sentence(self, tmp_path, monkeypatch):
        """数字强调：这一句起点前没有胶囊底，chip 淡入完成后**明显多出一大片**。

        数的颜色是**胶囊底色** `NUMBER_BG` —— 它是这一层唯一独有的颜色。
        实测（本机这段两秒的片子里，胶囊所在方块 ROI 14259 像素）：
        `before(t=2.86)=75`，`after(t=3.50)=1046`（13.9 倍）；整条字幕带 125 → 2044。
        剩下的那点零头是带子里文字的**抗锯齿边**恰好落在胶囊色的容差里，与是否弹胶囊无关。

        ⚠️ **容差只能取 3**：当年带子底色 `SUBTITLE_BG`(#f4f7fa) 与胶囊底只差 3.4 个亮度级，
        容差放到 10 会把整条带子都数进去（实测两帧都是 4729 个，两帧一样 —— 等于什么都没测，
        那正是「叠上去看不出来」这个 bug 当初躲掉检测的原因）。现在胶囊底换成了
        `NUMBER_BG`(#cfe0f8，差 24.8)，判据更稳，但这条注释留着提醒「别把容差放大」。
        这里的数字会随编码器量化小幅浮动（同一个 ROI 换一张底图，实测 222→1467 / 75→1046），
        所以判据留了余量：倍数 > 3、绝对数 > 500。
        """
        import app.services.video as video_module

        sentence = "准确率从 61% 提到了 67%。"
        beats = [
            {"text": self.FIRST, "image": "f1"},  # 第一句不含数字 → 它自己没有 chip 层
            {"text": sentence, "image": "f1"},    # 第二句含数字 → 有 chip 层
        ]

        # 顺手盯住「这条带子是按 chip=False 渲染的」：合成结束会把中间图层删掉，
        # 所以在这里把带子拷出来留证（`RenderedLayers` 的中间文件是临时的，见 compose_video 末尾）
        kept = tmp_path / "kept"
        kept.mkdir()
        chips: list[bool] = []
        real_band_layer = video_module._render_band_layer

        def spy(beat, path, *, layout, session, chip=True, hidden_from=None):
            produced = real_band_layer(
                beat, path, layout=layout, session=session, chip=chip, hidden_from=hidden_from
            )
            chips.append(chip)
            (kept / Path(produced).name).write_bytes(Path(produced).read_bytes())
            return produced

        monkeypatch.setattr(video_module, "_render_band_layer", spy)
        recorded: dict = {}
        _, video, _ = self._compose(
            tmp_path, name="chip", beats=beats, second=sentence, record=recorded
        )

        # 接线证据①：字幕带按 chip 参数渲染 —— 有 chip 层的那一句必须 chip=False
        assert chips == [True, False], f"字幕带的 chip 参数不对：{chips}"
        assert capsule_bbox(kept / "band-0000-1.png") is None, "带子里不该再有那一颗胶囊"

        # 接线证据②：强调层真的交到了编码侧（不是躺在磁盘上没人用）
        chip_rows = recorded["beat_chips"][0]
        assert chip_rows[0] is None, "第一句没有阿拉伯数字，不该有强调层"
        # 不查文件在不在：合成结束会把中间图层删掉（`compose_video` 末尾的清理），
        # 这里要证明的是「它被**交给了编码**」，而它到底有没有进画面由下面两帧的像素说了算。
        assert chip_rows[1] is not None, "含数字的那一句应当有一个强调层"
        assert recorded["beat_cards"][0] == [None, None], "两句同一张图，不该有逐句卡"

        chip = render_caption_chip(sentence, tmp_path / "probe-chip.png")
        assert chip is not None
        box = capsule_bbox(chip)
        assert box is not None
        roi = (box[0] - 2, SUBTITLE_TOP + box[1] - 2, box[2] + 3, SUBTITLE_TOP + box[3] + 3)

        # 第二句起点 ≈ 2.909 秒；chip 的弹出窗口是 [3.029, 3.249]
        before = count_capsule_pixels(extract_frame(video, 2.86, tmp_path / "b.png"), roi)
        after = count_capsule_pixels(extract_frame(video, 3.50, tmp_path / "a.png"), roi)

        assert after > before * 3, f"胶囊底像素没有明显变多：{before} → {after}"
        assert after > 500, f"淡入完成后该有一整颗胶囊：{after}"
