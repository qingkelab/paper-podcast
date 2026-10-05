"""接口集成测试。全程走 Mock 模式，不依赖任何外部密钥。

这里覆盖的是「契约」：状态机推进、404/400/409 的边界、Range 请求、
下载响应头。这些是前端直接依赖的行为。
"""

from __future__ import annotations

import time

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
    settings = Settings(
        force_mock=True,
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
        assert episode["audio_url"] == f"/api/episodes/{created['id']}/audio"
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
