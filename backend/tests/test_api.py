"""接口集成测试。全程走 Mock 模式，不依赖任何外部密钥。

这里覆盖的是「契约」：状态机推进、404/400/409 的边界、Range 请求、
下载响应头。这些是前端直接依赖的行为。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SAMPLE_TEXT = (
    "Attention Is All You Need. The dominant sequence transduction models are based on "
    "complex recurrent or convolutional neural networks that include an encoder and a decoder. "
    "We propose a new simple network architecture, the Transformer, based solely on attention "
    "mechanisms, dispensing with recurrence and convolutions entirely. Experiments on two machine "
    "translation tasks show these models to be superior in quality while being more parallelizable "
    "and requiring significantly less time to train. Our model achieves 28.4 BLEU on the WMT 2014 "
    "English-to-German translation task, improving over the existing best results by over 2 BLEU. "
    "On the WMT 2014 English-to-French translation task, our model establishes a new single-model "
    "state-of-the-art BLEU score of 41.8 after training for 3.5 days on eight GPUs."
)


@pytest.fixture
def client(tmp_path):
    """常规用例：**关掉视频合成**。

    视频编码是 CPU 密集的，每个用例都编一遍会让整个测试套件慢两倍以上，
    而绝大多数用例并不关心视频。视频链路由 TestVideoPipeline 专门覆盖。
    """
    settings = Settings(
        force_mock=True,
        enable_video=False,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "test.db",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def video_client(tmp_path):
    """开启视频合成的客户端，只给视频集成测试用。"""
    from app.services.video import ffmpeg_available

    if not ffmpeg_available():
        pytest.skip("需要系统安装 ffmpeg")

    settings = Settings(
        force_mock=True,
        enable_video=True,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "test.db",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def wait_for_completion(client: TestClient, episode_id: str, timeout: float = 60.0) -> dict:
    """轮询到终态。Mock 模式下通常 2 秒内完成。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/episodes/{episode_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.2)
    pytest.fail(f"任务 {episode_id} 超时未完成，最后状态：{last.get('status')}")


def create_text_episode(client: TestClient, **overrides) -> dict:
    payload = {"source_type": "text", "text": SAMPLE_TEXT}
    payload.update(overrides)
    response = client.post("/api/episodes", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def make_pdf(text: str) -> bytes:
    """构造一个 pypdf 能提取出文字的合法 PDF。

    用来覆盖「上传 PDF」这条主路径——这是产品最重要的入口，
    必须有一个真实 PDF 走通全流程的回归测试。
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        if len(current) + len(word) > 80:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)

    content = "BT /F1 11 Tf 50 750 Td 14 TL\n"
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({escaped}) Tj T*\n"
    content += "ET"
    stream = content.encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_position = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF\n"
    ).encode()
    return bytes(out)


# --------------------------------------------------------------------------
# 基础接口
# --------------------------------------------------------------------------


class TestBasics:
    def test_health_reports_mock_modes(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["modes"] == {"llm": "mock", "tts": "mock"}

    def test_options_shape(self, client):
        body = client.get("/api/options").json()
        assert [d["value"] for d in body["durations"]] == [3, 5, 10]
        assert len(body["levels"]) == 3
        assert len(body["voices"]) >= 2
        for voice in body["voices"]:
            assert {"id", "label", "gender", "pair"} <= set(voice)


# --------------------------------------------------------------------------
# 创建任务
# --------------------------------------------------------------------------


class TestCreateEpisode:
    def test_create_from_text_returns_queued(self, client):
        episode = create_text_episode(client)
        assert episode["status"] == "queued"
        assert episode["progress"] == 0
        assert episode["id"]
        assert episode["audio_url"] is None

    def test_rejects_short_text(self, client):
        response = client.post(
            "/api/episodes", json={"source_type": "text", "text": "太短了"}
        )
        assert response.status_code == 400
        assert "太短" in response.json()["detail"]

    def test_rejects_unknown_source_type(self, client):
        response = client.post(
            "/api/episodes", json={"source_type": "magic", "text": SAMPLE_TEXT}
        )
        assert response.status_code == 400

    def test_rejects_bad_url_scheme(self, client):
        response = client.post(
            "/api/episodes", json={"source_type": "url", "url": "ftp://example.com/x.pdf"}
        )
        assert response.status_code == 400
        assert "http" in response.json()["detail"]

    def test_rejects_missing_url(self, client):
        response = client.post("/api/episodes", json={"source_type": "url"})
        assert response.status_code == 400

    def test_rejects_non_pdf_upload(self, client):
        response = client.post(
            "/api/episodes",
            files={"file": ("notes.txt", b"hello world", "text/plain")},
        )
        assert response.status_code == 400

    def test_rejects_fake_pdf_extension(self, client):
        """扩展名是 .pdf 但内容不是 PDF，应当被文件头校验拦下。"""
        response = client.post(
            "/api/episodes",
            files={"file": ("paper.pdf", b"this is not a pdf at all", "application/pdf")},
        )
        assert response.status_code == 400
        assert "PDF" in response.json()["detail"]

    def test_options_are_validated_and_defaulted(self, client):
        """越界的时长/难度/音色必须回退到默认值，不能透传给下游 API。"""
        episode = create_text_episode(
            client,
            options={"duration_min": 999, "level": "神级", "voice_a": "不存在的音色"},
        )
        assert episode["options"]["duration_min"] == 5
        assert episode["options"]["level"] == "intro"
        assert episode["options"]["voice_a"] != "不存在的音色"
        assert episode["options"]["voice_a"].endswith("_bigtts")

    def test_accepts_valid_options(self, client):
        episode = create_text_episode(
            client, options={"duration_min": 10, "level": "expert"}
        )
        assert episode["options"]["duration_min"] == 10
        assert episode["options"]["level"] == "expert"


# --------------------------------------------------------------------------
# 完整流水线
# --------------------------------------------------------------------------


class TestFullPipeline:
    def test_text_to_podcast_end_to_end(self, client):
        created = create_text_episode(client)
        episode = wait_for_completion(client, created["id"])

        assert episode["status"] == "completed", episode.get("error")
        assert episode["progress"] == 100
        assert episode["stage_label"] == "已完成"
        assert episode["error"] is None

        # 标题来自模型解读出的 paper_meta
        assert episode["title"]
        assert episode["paper_meta"] is not None
        assert episode["analysis"] is not None
        assert episode["analysis"]["innovations"]

        # 双人脚本
        script = episode["script"]
        assert len(script["segments"]) >= 4
        assert {s["speaker"] for s in script["segments"]} == {"A", "B"}
        assert [s["round"] for s in script["segments"]] == list(range(len(script["segments"])))
        assert script["word_count"] > 0
        assert script["est_duration_sec"] > 0

        # 音频产物
        # URL 上带 ?v= 版本号（内容变化时用于击穿缓存）
        assert episode["audio_url"].startswith(f"/api/episodes/{created['id']}/audio")
        assert episode["audio_bytes"] > 0
        assert episode["audio_duration_sec"] > 0

    def test_stage_progression_is_monotonic(self, client):
        """进度只能单调上升——前端进度条会跳变，不能倒退。"""
        created = create_text_episode(client)
        seen: list[int] = []
        deadline = time.time() + 30
        while time.time() < deadline:
            episode = client.get(f"/api/episodes/{created['id']}").json()
            seen.append(episode["progress"])
            if episode["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert seen == sorted(seen), f"进度出现回退：{seen}"

    def test_audio_is_served_with_range_support(self, client):
        """播放器拖进度条依赖 206 + Content-Range，这条不能坏。"""
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        full = client.get(f"/api/episodes/{created['id']}/audio")
        assert full.status_code == 200
        assert full.headers["accept-ranges"] == "bytes"
        assert len(full.content) > 1000

        partial = client.get(
            f"/api/episodes/{created['id']}/audio", headers={"Range": "bytes=0-1023"}
        )
        assert partial.status_code == 206
        assert len(partial.content) == 1024
        assert partial.headers["content-range"].startswith("bytes 0-1023/")
        assert partial.content == full.content[:1024]

    def test_suffix_range_request(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])
        response = client.get(
            f"/api/episodes/{created['id']}/audio", headers={"Range": "bytes=-100"}
        )
        assert response.status_code == 206
        assert len(response.content) == 100

    def test_script_download(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        response = client.get(f"/api/episodes/{created['id']}/script.txt")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert "【主播A】" in response.text
        assert "【主播B】" in response.text

    def test_analysis_download(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        response = client.get(f"/api/episodes/{created['id']}/analysis.md")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.text.startswith("# ")
        assert "## 核心创新点" in response.text


# --------------------------------------------------------------------------
# 列表 / 详情 / 删除 / 重试
# --------------------------------------------------------------------------


class TestManagement:
    def test_list_omits_large_fields(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        body = client.get("/api/episodes").json()
        assert body["total"] >= 1
        item = next(i for i in body["items"] if i["id"] == created["id"])
        # 列表页不返回大字段，避免列表接口变成几 MB
        assert item["analysis"] is None
        assert item["script"] is None
        assert item["figures"] == []
        assert item["illustration"] is None
        # 但封面要保留，列表卡片要显示缩略图
        assert "cover_url" in item

    def test_list_search_and_filter(self, client):
        episode = create_text_episode(client, title="独一无二的标题XYZ")
        wait_for_completion(client, episode["id"])

        assert client.get("/api/episodes", params={"q": "独一无二"}).json()["total"] == 1
        assert client.get("/api/episodes", params={"q": "绝不存在"}).json()["total"] == 0
        assert client.get("/api/episodes", params={"status": "completed"}).json()["total"] >= 1
        assert client.get("/api/episodes", params={"status": "failed"}).json()["total"] == 0

    def test_list_rejects_unknown_status(self, client):
        assert client.get("/api/episodes", params={"status": "乱写"}).status_code == 400

    def test_detail_returns_large_fields(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])
        detail = client.get(f"/api/episodes/{created['id']}").json()
        assert detail["analysis"] is not None
        assert detail["script"] is not None

    def test_404_for_unknown_id(self, client):
        assert client.get("/api/episodes/deadbeef").status_code == 404
        assert client.delete("/api/episodes/deadbeef").status_code == 404
        assert client.get("/api/episodes/deadbeef/audio").status_code == 404

    def test_audio_404_before_ready(self, client):
        """还没合成完就去取音频，应该是 404 而不是 500。"""
        created = create_text_episode(client)
        # 不等待完成
        assert client.get(f"/api/episodes/{created['id']}/audio").status_code == 404

    def test_delete_removes_episode_and_audio(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        assert client.delete(f"/api/episodes/{created['id']}").status_code == 204
        assert client.get(f"/api/episodes/{created['id']}").status_code == 404
        assert client.get(f"/api/episodes/{created['id']}/audio").status_code == 404

    def test_retry_rejected_when_not_failed(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])
        response = client.post(f"/api/episodes/{created['id']}/retry")
        assert response.status_code == 409
        assert "只有失败的任务" in response.json()["detail"]

    def test_retry_requeues_failed_episode(self, client):
        """构造一个必然失败的任务（PDF 内容无效），然后重试。"""
        response = client.post(
            "/api/episodes",
            files={
                "file": (
                    "broken.pdf",
                    b"%PDF-1.4\nthis is not really a pdf\n%%EOF",
                    "application/pdf",
                )
            },
        )
        assert response.status_code == 201
        episode_id = response.json()["id"]

        failed = wait_for_completion(client, episode_id)
        assert failed["status"] == "failed", "无效 PDF 应当失败"
        assert failed["error"]

        retry = client.post(f"/api/episodes/{episode_id}/retry")
        assert retry.status_code == 200
        assert retry.json()["status"] == "queued"
        assert retry.json()["error"] is None


# --------------------------------------------------------------------------
# PDF 上传主路径
# --------------------------------------------------------------------------


class TestPdfUpload:
    def test_real_pdf_runs_full_pipeline(self, client):
        """上传 PDF 是这个产品最重要的入口，必须有真实 PDF 走通全流程的回归测试。

        （此前这里有一个 bug：用 fastapi.UploadFile 对 starlette 的实例做 isinstance，
        由于前者是后者的子类，判断恒为假，导致所有 PDF 上传都被 400 拒绝。）
        """
        pdf_bytes = make_pdf(SAMPLE_TEXT * 2)
        response = client.post(
            "/api/episodes",
            files={"file": ("attention.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["source_type"] == "pdf"
        # 标题初始来自文件名，模型解读后会被替换成论文正式标题
        assert created["title"] == "attention"

        episode = wait_for_completion(client, created["id"])
        assert episode["status"] == "completed", episode.get("error")
        assert episode["paper_meta"]["title"]
        assert episode["script"]["segments"]
        assert episode["audio_url"]

    def test_pdf_options_are_applied(self, client):
        pdf_bytes = make_pdf(SAMPLE_TEXT * 2)
        response = client.post(
            "/api/episodes",
            files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
            data={"duration_min": "10", "level": "expert"},
        )
        assert response.status_code == 201
        options = response.json()["options"]
        assert options["duration_min"] == 10
        assert options["level"] == "expert"

    def test_uploaded_file_is_stored_under_episode_id(self, client, tmp_path):
        """上传文件用 episode id 命名，避免同名 PDF 互相覆盖。"""
        pdf_bytes = make_pdf(SAMPLE_TEXT * 2)
        first = client.post(
            "/api/episodes", files={"file": ("same.pdf", pdf_bytes, "application/pdf")}
        ).json()
        second = client.post(
            "/api/episodes", files={"file": ("same.pdf", pdf_bytes, "application/pdf")}
        ).json()
        assert first["id"] != second["id"]


# --------------------------------------------------------------------------
# 单端口部署：前端静态托管 + SPA 深链接回退
# --------------------------------------------------------------------------


class TestSpaFallback:
    """生产部署时前端构建产物由后端托管，history 模式路由必须能刷新。

    这里有真实的坑：Vue Router 用 createWebHistory（无 # 的干净 URL），
    用户在 /episode/abc 上按刷新会把请求打到后端，裸 StaticFiles 直接 404，
    表现为「刷新就白屏」。
    """

    @pytest.fixture
    def spa_client(self, tmp_path, monkeypatch):
        import app.main as main_module

        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html><body>SPA_SHELL</body></html>")
        (dist / "assets").mkdir()
        (dist / "assets" / "app.js").write_text("console.log(1)")

        monkeypatch.setattr(main_module, "FRONTEND_DIST", dist)

        settings = Settings(
            force_mock=True,
            data_dir=tmp_path / "data",
            database_path=tmp_path / "data" / "t.db",
        )
        app = main_module.create_app(settings)
        with TestClient(app) as client:
            yield client

    def test_serves_index_at_root(self, spa_client):
        response = spa_client.get("/")
        assert response.status_code == 200
        assert "SPA_SHELL" in response.text

    def test_serves_hashed_assets(self, spa_client):
        response = spa_client.get("/assets/app.js")
        assert response.status_code == 200
        assert "console.log" in response.text

    def test_deep_link_falls_back_to_index(self, spa_client):
        """这是关键用例：直接访问前端路由路径要返回 index.html 而不是 404。"""
        for path in ("/library", "/settings", "/task/abc123", "/episode/abc123"):
            response = spa_client.get(path)
            assert response.status_code == 200, f"{path} 应当回退到 index.html"
            assert "SPA_SHELL" in response.text

    def test_api_404_stays_json(self, spa_client):
        """/api/* 的 404 不能被回退成 HTML，否则前端拿到一坨 HTML 无法解析。"""
        response = spa_client.get("/api/episodes/doesnotexist")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert "SPA_SHELL" not in response.text

    def test_api_still_works_with_frontend_mounted(self, spa_client):
        assert spa_client.get("/api/health").json()["status"] == "ok"


# --------------------------------------------------------------------------
# 配图资源
# --------------------------------------------------------------------------


class TestEpisodeAssets:
    def test_text_episode_gets_generated_cover(self, client):
        """纯文本来源没有 PDF，封面应当退回用生成的信息图，不能为空。"""
        created = create_text_episode(client)
        episode = wait_for_completion(client, created["id"])

        assert episode["cover_url"].startswith(f"/api/episodes/{created['id']}/cover")
        assert episode["cover_width"] and episode["cover_height"]

        illustration = episode["illustration"]
        assert illustration is not None
        assert illustration["source"] in ("model", "fallback")
        # Mock 模式下没有真实模型，应当是本地兜底图
        assert illustration["source"] == "fallback"

    def test_cover_endpoint_serves_png(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        response = client.get(f"/api/episodes/{created['id']}/cover")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_illustration_png_and_svg(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        png = client.get(f"/api/episodes/{created['id']}/illustration.png")
        assert png.status_code == 200
        assert png.content[:8] == b"\x89PNG\r\n\x1a\n"

        svg = client.get(f"/api/episodes/{created['id']}/illustration.svg")
        assert svg.status_code == 200
        assert "image/svg+xml" in svg.headers["content-type"]
        assert "<svg" in svg.text
        # 模型生成的内容，必须带防脚本执行的兜底头
        assert "Content-Security-Policy" in svg.headers

    def test_assets_404_for_unknown_episode(self, client):
        assert client.get("/api/episodes/nope/cover").status_code == 404
        assert client.get("/api/episodes/nope/illustration.png").status_code == 404
        assert client.get("/api/episodes/nope/illustration.svg").status_code == 404
        assert client.get("/api/episodes/nope/figures/f1").status_code == 404

    def test_unknown_figure_404(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])
        response = client.get(f"/api/episodes/{created['id']}/figures/nosuch")
        assert response.status_code == 404

    def test_pdf_episode_extracts_cover_and_figures(self, client, tmp_path):
        """上传含图形的 PDF，应当产出封面 + 原图。"""
        import pymupdf

        # 需要同时满足两个条件：有矢量图形（才提得到图）、正文够 200 字
        # （否则会被当成扫描版 PDF 拒掉）
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
        page.draw_rect(pymupdf.Rect(120, 160, 300, 300), fill=(0.75, 0.82, 0.93))
        page.draw_line(pymupdf.Point(300, 230), pymupdf.Point(460, 230), width=2)
        page.insert_text((80, 410), "Figure 1: The overall architecture of our model.", fontsize=10)
        for line in range(14):
            page.insert_text(
                (80, 450 + line * 14),
                "We propose a simple and effective approach for sequence modeling. "
                "Experiments show consistent improvements over strong baselines.",
                fontsize=9,
            )
        pdf_bytes = doc.tobytes()
        doc.close()

        response = client.post(
            "/api/episodes",
            files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 201
        created = response.json()

        # 封面在解析阶段就生成，不必等整条流水线跑完
        deadline = time.time() + 30
        episode = {}
        while time.time() < deadline:
            episode = client.get(f"/api/episodes/{created['id']}").json()
            if episode["cover_url"]:
                break
            time.sleep(0.2)

        assert episode["cover_url"], "PDF 来源应当有封面"
        assert episode["cover_width"] > 300
        assert client.get(f"/api/episodes/{created['id']}/cover").status_code == 200

    def test_delete_removes_asset_files(self, client):
        created = create_text_episode(client)
        wait_for_completion(client, created["id"])
        assert client.get(f"/api/episodes/{created['id']}/cover").status_code == 200

        assert client.delete(f"/api/episodes/{created['id']}").status_code == 204
        assert client.get(f"/api/episodes/{created['id']}/cover").status_code == 404


# --------------------------------------------------------------------------
# 视频解读播客（集成）
# --------------------------------------------------------------------------


class TestVideoPipeline:
    """视频链路的端到端覆盖。

    常规用例都把 enable_video 关掉了（太慢），所以这里单独验一次：
    音频合成完成后要真的产出一个能播的 MP4，而且长度和音频对得上。
    """

    def test_episode_gets_a_video(self, video_client):
        created = create_text_episode(video_client)
        episode = wait_for_completion(video_client, created["id"], timeout=180)

        assert episode["status"] == "completed", episode.get("error")
        video = episode.get("video")
        assert video is not None, "应当产出视频"
        assert video["url"].startswith(f"/api/episodes/{created['id']}/video")
        assert video["scene_count"] > 0
        assert video["bytes"] > 1000

    def test_video_is_served_with_range_support(self, video_client):
        created = create_text_episode(video_client)
        wait_for_completion(video_client, created["id"], timeout=180)

        full = video_client.get(f"/api/episodes/{created['id']}/video")
        assert full.status_code == 200
        assert full.headers["content-type"] == "video/mp4"
        assert full.headers["accept-ranges"] == "bytes"
        assert full.content[4:8] == b"ftyp", "应当是标准 MP4 容器"

        partial = video_client.get(
            f"/api/episodes/{created['id']}/video", headers={"Range": "bytes=0-1023"}
        )
        assert partial.status_code == 206
        assert partial.headers["content-range"].startswith("bytes 0-1023/")

    def test_video_length_matches_audio(self, video_client):
        """视频长度必须和音频基本一致，否则结尾会出现「只有画面没有声音」。"""
        import subprocess

        created = create_text_episode(video_client)
        episode = wait_for_completion(video_client, created["id"], timeout=180)

        path = Path(video_client.app.state.settings.video_dir) / f"{created['id']}.mp4"
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=duration",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        video_duration = float(probe)
        audio_duration = episode["audio_duration_sec"]

        assert abs(video_duration - audio_duration) < 0.5, (
            f"视频 {video_duration}s 与音频 {audio_duration}s 差得太多"
        )

    def test_list_omits_video(self, video_client):
        """列表接口不带 video（列表里不播视频）。"""
        created = create_text_episode(video_client)
        wait_for_completion(video_client, created["id"], timeout=180)

        body = video_client.get("/api/episodes").json()
        item = next(i for i in body["items"] if i["id"] == created["id"])
        assert item["video"] is None

    def test_delete_removes_video_file(self, video_client):
        created = create_text_episode(video_client)
        wait_for_completion(video_client, created["id"], timeout=180)
        path = Path(video_client.app.state.settings.video_dir) / f"{created['id']}.mp4"
        assert path.exists()

        assert video_client.delete(f"/api/episodes/{created['id']}").status_code == 204
        assert not path.exists()

    def test_video_disabled_produces_no_video(self, client):
        """默认（关闭视频）时不应该有视频，也不该报错。"""
        created = create_text_episode(client)
        episode = wait_for_completion(client, created["id"])
        assert episode["status"] == "completed"
        assert episode["video"] is None
        assert client.get(f"/api/episodes/{created['id']}/video").status_code == 404


class TestFigureWipeGuard:
    """提取不到配图时，绝不能把已有的配图清空。

    这不是假想问题：我在手工重新提取某一集的配图时踩过 —— 抓错了 URL
    （abs 页面而不是 PDF），提取到 0 张，然后直接把 0 张写回了数据库，
    那一集的 5 张配图当场没了。生产线路径有 `if figures:` 保护，这里把它锁住。
    """

    def test_pipeline_keeps_figures_when_extraction_returns_none(self, client, monkeypatch):
        import app.services.pipeline as pipeline_module

        created = create_text_episode(client)
        wait_for_completion(client, created["id"])

        # 先塞入几张「已有配图」
        db = client.app.state.db
        existing = [
            {
                "id": "f1",
                "kind": "figure",
                "label": "Figure 1",
                "caption": "c",
                "page": 1,
                "path": __file__,
                "width": 10,
                "height": 10,
            }
        ]
        db.update_episode(created["id"], figures=existing)

        # 让提取器返回空
        monkeypatch.setattr(pipeline_module, "extract_figures", lambda *a, **k: [])

        # 再跑一次流水线（重试路径）
        db.update_episode(created["id"], status="failed", error="x")
        client.post(f"/api/episodes/{created['id']}/retry")
        wait_for_completion(client, created["id"])

        kept = db.get_episode(created["id"])["figures"]
        assert kept, "提取返回空时不该把已有配图清掉"


# --------------------------------------------------------------------------
# 人工校正配图
# --------------------------------------------------------------------------


class TestManualFigureCorrection:
    """自动判定不可能总对 —— 「图正不正」最终要靠人眼。

    所以给一个手动兜底：看的人觉得歪了就转一下。这是我自己看不了图
    这个限制的正解，也顺带让提取错方向的图能被人工修回来。
    """

    @staticmethod
    def _episode_with_figures(client, count: int = 2) -> str:
        import pymupdf

        created = create_text_episode(client)
        wait_for_completion(client, created["id"])
        db = client.app.state.db
        settings = client.app.state.settings

        figures = []
        for index in range(count):
            path = settings.figure_dir / f"{created['id']}-f{index + 1}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), False)
            pix.save(str(path))
            figures.append(
                {
                    "id": f"f{index + 1}",
                    "kind": "figure",
                    "label": f"Figure {index + 1}",
                    "caption": "c",
                    "page": 1,
                    "path": str(path),
                    "width": 300,
                    "height": 200,
                }
            )
        db.update_episode(created["id"], figures=figures)
        return created["id"]

    def test_rotate_clockwise_swaps_dimensions(self, client):
        episode_id = self._episode_with_figures(client)
        response = client.post(
            f"/api/episodes/{episode_id}/figures/f1/rotate", json={"direction": "cw"}
        )
        assert response.status_code == 200, response.text
        figure = next(f for f in response.json()["figures"] if f["id"] == "f1")
        assert (figure["width"], figure["height"]) == (200, 300)

    def test_rotate_is_reversible(self, client):
        """PNG 转 90° 无损，转多了转回来即可 —— 四圈应当回到原点。"""
        episode_id = self._episode_with_figures(client)
        for _ in range(4):
            client.post(
                f"/api/episodes/{episode_id}/figures/f1/rotate", json={"direction": "cw"}
            )
        figure = next(
            f
            for f in client.get(f"/api/episodes/{episode_id}").json()["figures"]
            if f["id"] == "f1"
        )
        assert (figure["width"], figure["height"]) == (300, 200)

    def test_counter_clockwise_undoes_clockwise(self, client):
        episode_id = self._episode_with_figures(client)
        client.post(f"/api/episodes/{episode_id}/figures/f1/rotate", json={"direction": "cw"})
        response = client.post(
            f"/api/episodes/{episode_id}/figures/f1/rotate", json={"direction": "ccw"}
        )
        figure = next(f for f in response.json()["figures"] if f["id"] == "f1")
        assert (figure["width"], figure["height"]) == (300, 200)

    def test_rotation_changes_url_version(self, client):
        """旋转后 URL 上的版本号必须变化，否则浏览器会继续用缓存的旧图。

        这正是之前那个坑：图重生成过，但 URL 没变、又设了 24 小时缓存，
        用户一直看到旧图，白排查一轮。
        """
        episode_id = self._episode_with_figures(client)
        before = next(
            f for f in client.get(f"/api/episodes/{episode_id}").json()["figures"]
            if f["id"] == "f1"
        )["url"]

        response = client.post(
            f"/api/episodes/{episode_id}/figures/f1/rotate", json={"direction": "cw"}
        )
        after = next(f for f in response.json()["figures"] if f["id"] == "f1")["url"]
        assert before != after, "旋转后 URL 版本号没变，缓存不会失效"

    def test_asset_urls_carry_version(self, client):
        """封面与配图 URL 都应带版本号。"""
        episode_id = self._episode_with_figures(client)
        body = client.get(f"/api/episodes/{episode_id}").json()
        assert "?v=" in body["figures"][0]["url"]
        if body["cover_url"]:
            assert "?v=" in body["cover_url"]

    def test_invalid_direction_rejected(self, client):
        episode_id = self._episode_with_figures(client)
        response = client.post(
            f"/api/episodes/{episode_id}/figures/f1/rotate", json={"direction": "upside"}
        )
        assert response.status_code == 422

    def test_rotate_unknown_figure_404(self, client):
        episode_id = self._episode_with_figures(client)
        response = client.post(
            f"/api/episodes/{episode_id}/figures/nope/rotate", json={"direction": "cw"}
        )
        assert response.status_code == 404

    def test_delete_figure_removes_it(self, client):
        episode_id = self._episode_with_figures(client, count=2)
        response = client.delete(f"/api/episodes/{episode_id}/figures/f1")
        assert response.status_code == 200
        ids = [f["id"] for f in response.json()["figures"]]
        assert ids == ["f2"]

    def test_delete_unknown_figure_404(self, client):
        episode_id = self._episode_with_figures(client)
        assert client.delete(f"/api/episodes/{episode_id}/figures/nope").status_code == 404


@pytest.fixture
def bilingual_client(tmp_path):
    """双语模式：每期同时产出中文版和英文版。"""
    settings = Settings(
        force_mock=True,
        enable_video=False,
        languages="zh,en",
        default_language="zh",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "test.db",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


class TestBilingualVersions:
    """同一集内嵌中英两版，`?lang=` 切换。

    设计要点：顶层字段**镜像主语言**那一版，老前端不改也能用；
    另一语言只在 `versions[lang]` 里。
    """

    def _create_en_primary(self, client, **extra):
        payload = {
            "source_type": "text",
            "text": SAMPLE_TEXT,
            "options": {"duration_min": 3, "level": "intro", **extra},
        }
        response = client.post("/api/episodes", json=payload)
        assert response.status_code == 201, response.text
        return wait_for_completion(client, response.json()["id"])

    def test_both_languages_are_produced(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        assert episode["status"] == "completed", episode.get("error")
        assert episode["languages"] == ["zh", "en"]
        assert set(episode["versions"].keys()) == {"zh", "en"}

    def test_each_version_has_own_script_and_audio(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        zh, en = episode["versions"]["zh"], episode["versions"]["en"]

        assert zh["audio_url"] and en["audio_url"]
        assert zh["audio_url"] != en["audio_url"]
        assert zh["language"] == "zh" and en["language"] == "en"

        zh_text = " ".join(s["text"] for s in zh["script"]["segments"])
        en_text = " ".join(s["text"] for s in en["script"]["segments"])
        # 英文版必须是真英文，而不是把中文版复制一份
        assert "Today's paper" in en_text or "the paper" in en_text.lower()
        assert "今天聊的" in zh_text
        assert _cjk_ratio(en_text) < 0.1, "英文版里混进了中文"

    def test_top_level_mirrors_primary_language(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        assert episode["language"] == "zh"
        assert episode["script"] == episode["versions"]["zh"]["script"]
        assert episode["audio_url"] == episode["versions"]["zh"]["audio_url"]
        assert episode["options"]["language"] == "zh"
        assert episode["options"]["languages"] == ["zh", "en"]

    def test_english_primary_is_respected(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client, language="en", languages=["en"])
        assert episode["language"] == "en"
        assert episode["script"] == episode["versions"]["en"]["script"]
        assert "今天聊的" not in " ".join(
            s["text"] for s in episode["script"]["segments"]
        )

    def test_lang_query_switches_audio(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        path = f"/api/episodes/{episode['id']}/audio"

        zh = bilingual_client.get(path)
        en = bilingual_client.get(f"{path}?lang=en")
        assert zh.status_code == 200 and en.status_code == 200
        # 两版必须是**两个文件**：Mock TTS 只按段数与字数合成占位音，
        # 内容层面证明不了「英文版在念英文」，所以这里断言的是路径与时长各自独立。
        urls = {
            episode["versions"]["zh"]["audio_url"],
            episode["versions"]["en"]["audio_url"],
        }
        assert len(urls) == 2, urls
        assert episode["versions"]["zh"]["audio_duration_sec"] is not None
        assert episode["versions"]["en"]["audio_duration_sec"] is not None
        assert int(zh.headers["content-length"]) > 0

    def test_lang_query_switches_downloads(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        base = f"/api/episodes/{episode['id']}"

        zh_script = bilingual_client.get(f"{base}/script.txt").text
        en_script = bilingual_client.get(f"{base}/script.txt?lang=en").text
        assert "主播A" in zh_script
        assert "主播A" not in en_script
        assert "Today's paper" in en_script

        zh_md = bilingual_client.get(f"{base}/analysis.md").text
        en_md = bilingual_client.get(f"{base}/analysis.md?lang=en").text
        assert "研究背景" in zh_md
        assert "研究背景" not in en_md

    def test_english_download_has_language_suffix(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        response = bilingual_client.get(
            f"/api/episodes/{episode['id']}/script.txt?lang=en"
        )
        disposition = response.headers["content-disposition"]
        assert "-en-" in disposition, disposition

    def test_unknown_language_is_404(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        base = f"/api/episodes/{episode['id']}"
        assert bilingual_client.get(f"{base}/audio?lang=jp").status_code == 404
        assert bilingual_client.get(f"{base}/script.txt?lang=jp").status_code == 404
        assert bilingual_client.get(f"{base}/analysis.md?lang=jp").status_code == 404

    def test_legacy_episode_without_versions_still_works(self, client):
        """双语之前生成的集没有 versions，顶层就是唯一那一版。"""
        response = client.post(
            "/api/episodes",
            json={
                "source_type": "text",
                "text": SAMPLE_TEXT,
                "options": {"duration_min": 3, "level": "intro"},
            },
        )
        episode = wait_for_completion(client, response.json()["id"])
        assert episode["languages"] == ["zh"]
        assert episode["language"] == "zh"
        # 老数据也要能被 ?lang=zh 命中（前端切换器不必为空数据写特例）
        assert client.get(
            f"/api/episodes/{episode['id']}/audio?lang=zh"
        ).status_code == 200
        assert client.get(
            f"/api/episodes/{episode['id']}/audio?lang=en"
        ).status_code == 404

    def test_server_wide_bilingual_does_not_invent_english_for_single_lang_episode(
        self, bilingual_client
    ):
        """服务端支持中英双语 ≠ 每一集都有英文版。

        这个坑很隐蔽：拿服务端配置的语言列表去判「有没有这一版」，
        老数据的 `?lang=en` 会**静默返回中文内容**（200 + 主语言），
        前端以为切成英文了，其实一个字都没变。
        """
        # 明确只要中文版
        episode = self._create_en_primary(bilingual_client, languages=["zh"])
        assert episode["languages"] == ["zh"]
        assert episode["options"]["languages"] == ["zh"]

        base = f"/api/episodes/{episode['id']}"
        assert bilingual_client.get(f"{base}/audio?lang=en").status_code == 404
        assert bilingual_client.get(f"{base}/video?lang=en").status_code == 404
        assert bilingual_client.get(f"{base}/script.txt?lang=en").status_code == 404
        assert bilingual_client.get(f"{base}/analysis.md?lang=en").status_code == 404
        assert bilingual_client.get(f"{base}/audio?lang=zh").status_code == 200

    def test_planned_languages_visible_while_generating(self, bilingual_client):
        """任务还在跑时 `options.languages` 已经是要求的两版，`languages` 还没有。"""
        response = bilingual_client.post(
            "/api/episodes",
            json={
                "source_type": "text",
                "text": SAMPLE_TEXT,
                "options": {"duration_min": 3, "languages": ["zh", "en"]},
            },
        )
        created = response.json()
        assert created["options"]["languages"] == ["zh", "en"]
        assert created["languages"] == []

    def test_intro_outro_present_in_both_languages(self, bilingual_client):
        episode = self._create_en_primary(bilingual_client)
        for lang in ("zh", "en"):
            segments = episode["versions"][lang]["script"]["segments"]
            brands = [s.get("brand") for s in segments]
            assert "intro" in brands and "outro" in brands


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(text)


class TestTitleFromModel:
    """模型认出的正式标题要盖掉 `guess_title` 的启发式猜测。

    踩过的坑：双语改造时漏了这一步的写回，等于让启发式猜测永久生效。
    只在「猜错」时才看得出来 —— arXiv PDF 首页常把授权声明排在标题前面，
    实测抓到过一集标题是「Provided proper attribution is provided, Google hereby
    grants permission to」。
    """

    LICENSE_FIRST = (
        "Provided proper attribution is provided, Google hereby grants permission to "
        "reproduce the tables and figures in this paper solely for use in journalistic "
        "or scholarly works. The dominant sequence transduction models are based on complex "
        "recurrent or convolutional neural networks that include an encoder and a decoder. "
        "We propose a new simple network architecture, the Transformer, based solely on "
        "attention mechanisms, dispensing with recurrence and convolutions entirely."
    )

    def test_model_title_overrides_guess(self, client):
        response = client.post(
            "/api/episodes",
            json={
                "source_type": "text",
                "text": self.LICENSE_FIRST,
                "options": {"duration_min": 3, "level": "intro"},
            },
        )
        assert response.status_code == 201, response.text
        created = response.json()
        # 建任务那一刻用的还是启发式猜测 —— 关键是它**不等于**模型认出的正式标题，
        # 所以「写回」这一步是有意义的（不写回就永远停在这个猜测上）
        assert created["title"] != "Attention Is All You Need"

        finished = wait_for_completion(client, created["id"])
        assert finished["paper_meta"]["title"] == "Attention Is All You Need"
        # 跑完之后标题必须是模型认出的那个
        assert finished["title"] == "Attention Is All You Need"

    def test_user_supplied_title_is_respected(self, client):
        """用户自己填了标题就不许被模型改掉。"""
        response = client.post(
            "/api/episodes",
            json={
                "source_type": "text",
                "text": self.LICENSE_FIRST,
                "title": "我自己起的名字",
                "options": {"duration_min": 3, "level": "intro"},
            },
        )
        assert response.status_code == 201
        finished = wait_for_completion(client, response.json()["id"])
        assert finished["title"] == "我自己起的名字"


class TestWorkerRetryPolicy:
    """哪些错误值得重试。

    实测代价：DeepSeek 余额不足（402）曾经不在「不可重试」清单里，
    于是一次生成被**完整重跑 3 遍** —— 每遍重新下载 PDF、重新提取配图、
    重新调模型，最后拿到的还是同一个 402。白烧时间和带宽。
    """

    def test_balance_error_is_permanent(self):
        from app.worker import _is_permanent

        assert _is_permanent("DeepSeek 账户余额不足（402）")
        assert _is_permanent("402 Payment Required")

    def test_auth_and_parse_errors_stay_permanent(self):
        from app.worker import _is_permanent

        for message in ("鉴权失败", "无法解析这个 PDF", "论文有效正文过短", "接入点不存在"):
            assert _is_permanent(message), message

    def test_rate_limit_is_still_retryable(self):
        """429 限流是真能靠退避等过去的 —— 不能因为怕浪费就把它也算成永久错误。"""
        from app.worker import _is_permanent

        assert not _is_permanent("请求过于频繁（429）")
        assert not _is_permanent("连接超时")
        assert not _is_permanent("网络抖动")
