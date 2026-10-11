"""字幕按小句依次出现（`video_core.plan_reveals` + 带子的 `hidden_from`）。

**这是为了填掉「≥4 秒画面上没有任何变化」那些空档**（见 `docs/VIDEO_QA.md`）：
一句 30 字的话念 10 秒，原来整句在第 0.35 秒就全出现了，中间 10 秒什么都不变。
现在按小句依次显露，读到哪里字长到哪里。

两种断言都要，缺一不可：

- **时间表**（纯函数，不需要 Chrome）：短句不做、间隔够、最后一个状态一定「全都显示」；
- **画面里真的在变**（真出片 + 抽帧数墨迹）：时间表排得再漂亮，图层叠错了也是白排。
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from app.services.design import PORTRAIT, SUBTITLE_TOP
from app.services.htmlframe import ChromeSession, chrome_available
from app.services.video_core import CaptionBand, Scene, clause_spans, plan_reveals
from app.services.video_encode import encode_video, ffmpeg_available

requires_chrome = pytest.mark.skipif(
    not chrome_available(), reason="这台机器上没有 Chrome，跳过需要真渲染的用例"
)
requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="需要系统安装 ffmpeg"
)

SENTENCE = "它把权重压到 4 比特，精度只掉 0.3%，显存却只要三分之一。"
LONG = "先说结论，然后我们看数据，最后再回到方法本身，看看它到底改了什么。"


@pytest.fixture(scope="module")
def session():
    if not chrome_available():
        pytest.skip("这台机器上没有 Chrome")
    with ChromeSession.start() as active:
        yield active


def make_frame(path: Path) -> Path:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300), False)
    pix.set_rect(pix.irect, (250, 250, 250))
    pix.save(str(path))
    return path


def make_silence(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def extract(path: Path, at: float, out: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ss", f"{at:.3f}",
         "-frames:v", "1", str(out)],
        check=True,
    )
    return out


def band_ink(path: Path) -> int:
    """字幕带里「文字墨迹」的像素数（带子底色是 #f4f7fa，文字比它深得多）。"""
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    n, width, height, data = pix.n, pix.width, pix.height, pix.samples
    return sum(
        1
        for y in range(SUBTITLE_TOP, height)
        for x in range(width)
        if data[(y * width + x) * n] < 160
    )


class TestClauseSpans:
    def test_splits_at_commas_and_keeps_the_punctuation(self):
        spans = clause_spans(SENTENCE)
        assert [SENTENCE[a:b] for a, b in spans] == [
            "它把权重压到 4 比特，",
            "精度只掉 0.3%，",
            "显存却只要三分之一。",
        ]

    def test_no_punctuation_means_no_reveal(self):
        assert clause_spans("整句没有标点") == []

    def test_sentence_end_does_not_split(self):
        """句号不切：那是「另一句字幕」的事，`split_caption_beats` 管那一段。

        所以只有句号的文本切不出小句（返回空 = 不做显露），这不是 bug 而是判据的边界：
        「句号」在这里是**一句话的结尾**，而小句指的是同一句话里的停顿。
        """
        assert clause_spans("先说一句。再说一句。") == []
        assert clause_spans("先说一句。再说一句。") == []


class TestPlanReveals:
    def test_short_sentence_is_left_alone(self):
        assert plan_reveals(SENTENCE, 2.0) == []

    def test_no_punctuation_is_left_alone(self):
        assert plan_reveals("整句没有标点也没有停顿", 20.0) == []

    def test_the_last_state_always_shows_everything(self):
        """**最容易写错的地方**：最后一个状态必须「全都显示」。

        只按小句边界排的话，靠近句尾那一刀会被「末尾要留够时间」挡掉，
        那一小句就永远藏着 —— 观众最后看到的是半句话。
        """
        for text, duration in ((SENTENCE, 5.0), (SENTENCE, 8.0), (LONG, 12.0), (LONG, 20.0)):
            plan = plan_reveals(text, duration)
            assert plan, f"{duration} 秒的句子应当有显露计划"
            assert plan[-1][0] == len(clause_spans(text)), (
                f"最后一个状态没有「全都显示」：{plan} vs {len(clause_spans(text))} 个小句"
            )

    def test_hidden_from_and_moments_strictly_increase(self):
        plan = plan_reveals(LONG, 12.0)
        orders = [hidden_from for hidden_from, _ in plan]
        moments = [moment for _, moment in plan]
        assert orders == sorted(orders) and len(set(orders)) == len(orders)
        assert moments == sorted(moments) and len(set(moments)) == len(moments)

    def test_states_are_spread_not_clustered_at_the_front(self):
        """**踩过**：贪心取前几个边界，会让「全都显示」落在句子一半处，后半句又静止。

        4 个小句 12 秒的句子，实测是 1.8 / 4.7 / 8.4 —— 最后一个状态要落在后半段。
        """
        plan = plan_reveals(LONG, 12.0)
        assert plan[-1][1] > 12.0 * 0.5, f"最后一个小句太早出现：{plan}"

    def test_gap_between_states(self):
        from app.services.design import CLAUSE_REVEAL_MIN_GAP

        plan = plan_reveals(LONG, 20.0)
        for (_, first), (_, second) in zip(plan, plan[1:]):
            assert second - first >= CLAUSE_REVEAL_MIN_GAP * 0.5, f"两次显露挨太近：{plan}"

    def test_the_number_of_states_is_capped(self):
        from app.services.design import CLAUSE_REVEAL_MAX

        plan = plan_reveals("一，二，三，四，五，六，七，八，九，十，十一，十二，十三，十四。", 20.0)
        assert len(plan) <= CLAUSE_REVEAL_MAX

    def test_last_state_leaves_room_before_the_sentence_ends(self):
        from app.services.design import CLAUSE_REVEAL_TAIL_HOLD

        for duration in (5.0, 8.0, 20.0):
            plan = plan_reveals(LONG, duration)
            assert plan[-1][1] <= duration - CLAUSE_REVEAL_TAIL_HOLD + 0.01


@requires_chrome
class TestRevealKeepsTheLayoutStable:
    """小句显露**只改颜色，不改排版** —— 这是它能不能用的前提。

    字号自适应是在「文字全在」的前提下量出来的。要是把还没轮到的小句用
    `display:none` 藏掉，每一张带子的字号和换行都不一样，观众看到的是**字在跳**。
    """

    def _measure(self, session, tmp_path, hidden_from):
        import json

        from app.services.htmlpage import caption_band_page

        page = caption_band_page(SENTENCE, PORTRAIT, chip=False, hidden_from=hidden_from)
        session.open_page(page, work_dir=tmp_path)
        raw = session._evaluate(
            """(() => {
              const el = document.querySelector('.band-text');
              const style = getComputedStyle(el);
              const range = document.createRange();
              range.selectNodeContents(el);
              const rects = Array.from(range.getClientRects());
              return JSON.stringify({
                font: parseFloat(style.fontSize),
                lines: rects.length,
                height: Math.round(rects.reduce((m, r) => Math.max(m, r.bottom), 0)),
              });
            })()"""
        )
        return json.loads(raw)

    def test_font_size_and_line_count_do_not_change(self, session, tmp_path):
        full = self._measure(session, tmp_path, None)
        for hidden_from in (1, 2, 3):
            state = self._measure(session, tmp_path, hidden_from)
            assert state["font"] == full["font"], (
                f"藏起第 {hidden_from} 个小句之后字号变了：{state} vs {full}"
            )
            assert state["lines"] == full["lines"], (
                f"藏起第 {hidden_from} 个小句之后换行变了：{state} vs {full}"
            )
            assert abs(state["height"] - full["height"]) <= 2


@requires_chrome
@requires_ffmpeg
class TestRevealReachesTheVideo:
    """真出片 + 抽帧：字幕带里的墨迹要**按时间表**一级一级变多。

    只测时间表是不够的 —— 图层叠错、`enable` 时刻算错，时间表看起来都对，画面上什么都不变。
    实测（本机 8 秒的片段、三小句）：0.5s=1524 → 3.6s=2527 → 6.0s=3823 个墨迹像素。
    """

    def _encode(self, tmp_path, text: str, seconds: float) -> tuple[Path, list[tuple[int, float]]]:
        import app.services.video as video_module
        from app.services.video_render import render_caption_band

        frame = make_frame(tmp_path / "frame.png")
        audio = make_silence(tmp_path / "a.wav", seconds)
        plan = plan_reveals(text, seconds)
        plain = render_caption_band(text, tmp_path / "band.png", layout=PORTRAIT, chip=False)
        scene = Scene(
            start=0.0, end=seconds, image=frame, kind="figure", text=text, speaker="A"
        )
        session = ChromeSession.start()
        try:
            band = video_module._render_band_layer(
                text, tmp_path / "band.png", layout=PORTRAIT, session=session,
                chip=False, hidden_from=1 if plan else None,
            )
            reveals = tuple(
                (
                    video_module._render_band_layer(
                        text, tmp_path / f"band-r{index}.png", layout=PORTRAIT,
                        session=session, chip=False, hidden_from=hidden_from,
                    ),
                    moment,
                )
                for index, (hidden_from, moment) in enumerate(plan)
            )
        finally:
            session.close()
        encode_video(
            [scene],
            [plain],
            audio,
            tmp_path / "out.mp4",
            caption_bands=[[CaptionBand(text=text, image=band, reveals=reveals)]],
        )
        return tmp_path / "out.mp4", plan

    def test_the_text_grows_clause_by_clause(self, tmp_path):
        video, plan = self._encode(tmp_path, SENTENCE, 8.0)
        assert plan, "这句话应当有显露计划"
        moments = [moment for _, moment in plan]
        # 每个状态之后各抽一帧，墨迹必须**一级比一级多**
        ink = [
            band_ink(extract(video, 0.4, tmp_path / "a.png")),
            *[
                band_ink(extract(video, moment + 0.4, tmp_path / f"r{index}.png"))
                for index, moment in enumerate(moments)
            ],
        ]
        assert ink == sorted(ink), f"字幕没有逐句变多：{ink}（时间表 {moments}）"
        assert ink[-1] > ink[0] * 1.5, f"最后一张和第一张差不多：{ink}"
        # 每个中间状态都要**真的多出字**来（不是只有最后一张才补上）
        assert all(later > earlier for earlier, later in zip(ink, ink[1:])), ink

    def test_a_short_sentence_stays_still(self, tmp_path):
        """短句不做显露：整句一起出现（对它来说「一闪而过」比不动更难看）。"""
        video, plan = self._encode(tmp_path, "这就一句话。", 2.0)
        assert plan == []
        before = band_ink(extract(video, 0.4, tmp_path / "s1.png"))
        after = band_ink(extract(video, 1.6, tmp_path / "s2.png"))
        assert before == after, f"短句不该有变化：{before} → {after}"
