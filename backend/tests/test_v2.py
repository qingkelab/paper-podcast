"""V2 测试：账号与会话、归属隔离、专辑、分享、批量。

这一批的重点不是「功能能用」，而是**边界**：
别人能不能看到我的东西、我的链接失效后还能不能访问、
一个链接失败会不会拖垮整批、口令字段会不会漏出去。
"""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_lib
from app.config import Settings
from app.main import create_app

SAMPLE_TEXT = (
    "Attention Is All You Need. The dominant sequence transduction models are based on "
    "complex recurrent or convolutional neural networks that include an encoder and a decoder. "
    "We propose a new simple network architecture, the Transformer, based solely on attention "
    "mechanisms, dispensing with recurrence and convolutions entirely. Experiments on two machine "
    "translation tasks show these models to be superior in quality while being more parallelizable "
    "and requiring significantly less time to train. Our model achieves 28.4 BLEU on the WMT 2014 "
    "English-to-German translation task, improving over the existing best results by over 2 BLEU."
)

PASSWORD = "correct-horse-battery"


def make_app(tmp_path, **overrides):
    """建一个指向 tmp_path 数据库的应用。

    **多用户场景必须每个用户各建一个 app**：TestClient 退出时会走 lifespan 关闭连接，
    复用同一个 app 的第二个 client 会拿到 `Cannot operate on a closed database`。
    两个 app 指向同一个 database_path，数据是共享的。
    """
    settings = Settings(
        force_mock=True,
        enable_video=False,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "test.db",
        **overrides,
    )
    return create_app(settings)


@contextmanager
def as_user(tmp_path, username: str | None = None, **overrides):
    """以某个用户的身份开一个客户端。username=None 表示匿名（不登录）。"""
    app = make_app(tmp_path, **overrides)
    with TestClient(app) as client:
        user = register(client, username) if username else None
        yield client, user


def wait_done(client: TestClient, episode_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/episodes/{episode_id}").json()
        if last.get("status") in ("completed", "failed"):
            return last
        time.sleep(0.2)
    pytest.fail(f"任务 {episode_id} 超时：{last.get('status')}")


def register(client: TestClient, username: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": PASSWORD, "display_name": username.title()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_episode(client: TestClient, *, wait: bool = True) -> dict:
    response = client.post(
        "/api/episodes",
        json={
            "source_type": "text",
            "text": SAMPLE_TEXT,
            "options": {"duration_min": 3, "level": "intro"},
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    return wait_done(client, created["id"]) if wait else created


# --------------------------------------------------------------------------
# 口令哈希（纯函数，最该被钉死的部分）
# --------------------------------------------------------------------------


class TestPasswordHashing:
    def test_roundtrip(self):
        stored = auth_lib.hash_password("s3cret-pass")
        assert auth_lib.verify_password("s3cret-pass", stored)
        assert not auth_lib.verify_password("s3cret-pas", stored)
        assert not auth_lib.verify_password("", stored)

    def test_same_password_gives_different_hash(self):
        """必须加盐：两个用户用同一个口令，哈希不能一样（否则一眼看出谁和谁同口令）。"""
        a = auth_lib.hash_password("same-password")
        b = auth_lib.hash_password("same-password")
        assert a != b
        assert auth_lib.verify_password("same-password", a)
        assert auth_lib.verify_password("same-password", b)

    def test_hash_does_not_contain_plaintext(self):
        stored = auth_lib.hash_password("plaintext-secret")
        assert "plaintext-secret" not in stored
        assert stored.startswith("scrypt$")

    def test_garbage_stored_value_is_false_not_crash(self):
        """存储格式坏掉时返回 False —— 登录接口不能 500。"""
        for broken in ("", "whatever", "scrypt$bad", "bcrypt$1$2$3$4$5"):
            assert auth_lib.verify_password("x", broken) is False

    def test_username_rules(self):
        assert auth_lib.normalize_username("  Guo_01 ") == "guo_01"
        for bad in ("ab", "a" * 33, "has space", "中文名", "bad!"):
            with pytest.raises(auth_lib.AuthError):
                auth_lib.normalize_username(bad)

    def test_password_rules(self):
        with pytest.raises(auth_lib.AuthError):
            auth_lib.normalize_password("short")


# --------------------------------------------------------------------------
# 注册 / 登录 / 会话
# --------------------------------------------------------------------------


class TestAuth:
    @pytest.fixture
    def client(self, tmp_path):
        with TestClient(make_app(tmp_path)) as test_client:
            yield test_client

    def test_open_mode_before_any_user(self, client):
        """库里没用户时不需要登录 —— 刚部署完能直接用。"""
        health = client.get("/api/health").json()
        assert health["mode"] == "open"
        assert client.get("/api/episodes").status_code == 200
        assert client.get("/api/auth/me").status_code == 401

    def test_mode_flips_to_auth_after_register(self, client):
        register(client, "guo")
        assert client.get("/api/health").json()["mode"] == "auth"

    def test_register_sets_httponly_cookie(self, client):
        response = client.post(
            "/api/auth/register", json={"username": "guo", "password": PASSWORD}
        )
        assert response.status_code == 201
        cookie_header = response.headers["set-cookie"]
        assert auth_lib.COOKIE_NAME in cookie_header
        # httpOnly 必须是开的：否则 XSS 能直接读走会话
        assert "httponly" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower()

    def test_password_hash_never_leaves_the_server(self, client):
        payload = register(client, "guo")
        assert set(payload) == {"id", "username", "display_name", "created_at"}
        assert "password" not in response_text(client)

    def test_login_and_logout_roundtrip(self, client):
        register(client, "guo")
        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").status_code == 401

        bad = client.post(
            "/api/auth/login", json={"username": "guo", "password": "wrong-password"}
        )
        assert bad.status_code == 401
        unknown = client.post(
            "/api/auth/login", json={"username": "nobody", "password": PASSWORD}
        )
        assert unknown.status_code == 401
        # 提示必须一模一样，不能告诉对方是用户名错还是口令错
        assert bad.json()["detail"] == unknown.json()["detail"]

        ok = client.post(
            "/api/auth/login", json={"username": "guo", "password": PASSWORD}
        )
        assert ok.status_code == 200
        assert client.get("/api/auth/me").json()["username"] == "guo"

    def test_logout_is_idempotent(self, client):
        assert client.post("/api/auth/logout").status_code == 204
        assert client.post("/api/auth/logout").status_code == 204

    def test_duplicate_username_conflicts(self, client):
        register(client, "guo")
        again = client.post(
            "/api/auth/register", json={"username": "guo", "password": PASSWORD}
        )
        assert again.status_code == 409

    def test_signup_code_gate(self, tmp_path):
        with TestClient(make_app(tmp_path, signup_code="qingke2026")) as client:
            missing = client.post(
                "/api/auth/register", json={"username": "guo", "password": PASSWORD}
            )
            assert missing.status_code == 403
            wrong = client.post(
                "/api/auth/register",
                json={"username": "guo", "password": PASSWORD, "signup_code": "nope"},
            )
            assert wrong.status_code == 403
            ok = client.post(
                "/api/auth/register",
                json={
                    "username": "guo",
                    "password": PASSWORD,
                    "signup_code": "qingke2026",
                },
            )
            assert ok.status_code == 201

    def test_first_user_claims_orphan_episodes(self, tmp_path):
        """V1 时代的数据不能因为上 V2 变成谁也看不到的孤儿。"""
        with as_user(tmp_path) as (client, _):
            # 开放模式下（还没有用户）先造两集
            first = make_episode(client)
            second = make_episode(client)

        with as_user(tmp_path, "guo") as (client, _):
            ids = {item["id"] for item in client.get("/api/episodes").json()["items"]}
            assert {first["id"], second["id"]} <= ids

    def test_second_user_does_not_get_other_data(self, tmp_path):
        with as_user(tmp_path, "guo") as (client, _):
            mine = make_episode(client)

        with as_user(tmp_path, "someone-else") as (other, _):
            assert other.get("/api/auth/me").json()["username"] == "someone-else"
            assert other.get("/api/episodes").json()["total"] == 0
            # 别人的单集一律 404，不是 403 —— 403 会泄漏「这个 id 存在」
            assert other.get(f"/api/episodes/{mine['id']}").status_code == 404
            assert other.delete(f"/api/episodes/{mine['id']}").status_code == 404
            assert other.get(f"/api/episodes/{mine['id']}/audio").status_code == 404


class TestProtectedEndpoints:
    """有用户存在之后，未登录必须处处 401 —— 漏一个就是免费的下载通道。"""

    @pytest.fixture
    def anon(self, tmp_path):
        with as_user(tmp_path, "guo") as (owner, _):
            episode = make_episode(owner)
        with as_user(tmp_path) as (anonymous, _):
            yield anonymous, episode

    @pytest.mark.parametrize(
        "path",
        [
            "/api/episodes",
            "/api/albums",
            "/api/showcase",
        ],
    )
    def test_listing_requires_login(self, anon, path):
        client, _ = anon
        assert client.get(path).status_code in (401, 404)

    @pytest.mark.parametrize(
        "suffix",
        ["", "/audio", "/video", "/cover", "/script.txt", "/analysis.md", "/illustration.svg"],
    )
    def test_resources_require_login(self, anon, suffix):
        client, episode = anon
        response = client.get(f"/api/episodes/{episode['id']}{suffix}")
        assert response.status_code in (401, 404), f"{suffix} 没挡住未登录访问"

    def test_options_and_health_stay_public(self, anon):
        """首页要在登录前就能显示状态，这两个不能 401。"""
        client, _ = anon
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/options").status_code == 200


# --------------------------------------------------------------------------
# 专辑
# --------------------------------------------------------------------------


class TestAlbums:
    @pytest.fixture
    def client(self, tmp_path):
        with TestClient(make_app(tmp_path)) as test_client:
            register(test_client, "guo")
            yield test_client

    def test_create_and_list(self, client):
        created = client.post("/api/albums", json={"title": "Transformer 系列"})
        assert created.status_code == 201
        album = created.json()
        assert album["episode_count"] == 0
        assert album["cover_url"] is None
        assert [a["title"] for a in client.get("/api/albums").json()] == ["Transformer 系列"]

    def test_empty_title_rejected(self, client):
        assert client.post("/api/albums", json={"title": "   "}).status_code == 400

    def test_assign_and_count(self, client):
        episode = make_episode(client)
        album = client.post("/api/albums", json={"title": "T"}).json()

        response = client.post(
            f"/api/albums/{album['id']}/episodes", json={"episode_ids": [episode["id"]]}
        )
        assert response.status_code == 200
        detail = response.json()
        assert detail["episode_count"] == 1
        assert [e["id"] for e in detail["episodes"]] == [episode["id"]]
        # 专辑封面取里面最新一集的封面
        assert detail["cover_url"] is not None or episode["cover_url"] is None

    def test_assign_is_idempotent(self, client):
        episode = make_episode(client)
        album = client.post("/api/albums", json={"title": "T"}).json()
        for _ in range(2):
            client.post(
                f"/api/albums/{album['id']}/episodes",
                json={"episode_ids": [episode["id"]]},
            )
        assert client.get(f"/api/albums/{album['id']}").json()["episode_count"] == 1

    def test_remove_from_album_keeps_episode(self, client):
        episode = make_episode(client)
        album = client.post("/api/albums", json={"title": "T"}).json()
        client.post(
            f"/api/albums/{album['id']}/episodes", json={"episode_ids": [episode["id"]]}
        )
        response = client.delete(f"/api/albums/{album['id']}/episodes/{episode['id']}")
        assert response.status_code == 200
        assert response.json()["episode_count"] == 0
        # 单集本身还在，只是不属于任何专辑了
        assert client.get(f"/api/episodes/{episode['id']}").json()["album_id"] is None

    def test_delete_album_keeps_episodes(self, client):
        """专辑是分组不是容器：删合集不该把里面的播客一起删了。"""
        episode = make_episode(client)
        album = client.post("/api/albums", json={"title": "T"}).json()
        client.post(
            f"/api/albums/{album['id']}/episodes", json={"episode_ids": [episode["id"]]}
        )
        assert client.delete(f"/api/albums/{album['id']}").status_code == 204
        assert client.get(f"/api/episodes/{episode['id']}").status_code == 200

    def test_update_album(self, client):
        album = client.post("/api/albums", json={"title": "旧名字"}).json()
        response = client.patch(
            f"/api/albums/{album['id']}", json={"title": "新名字", "description": "说明"}
        )
        assert response.status_code == 200
        assert response.json()["title"] == "新名字"
        assert response.json()["description"] == "说明"

    def test_other_user_cannot_touch_my_album(self, tmp_path):
        # 不依赖 client fixture：它已经注册过 guo，这里要自己控制两个账号
        with as_user(tmp_path, "guo") as (owner, _):
            album = owner.post("/api/albums", json={"title": "私有合集"}).json()
            episode = make_episode(owner)

        with as_user(tmp_path, "intruder") as (other, _):
            assert other.get(f"/api/albums/{album['id']}").status_code == 404
            assert other.patch(
                f"/api/albums/{album['id']}", json={"title": "改了"}
            ).status_code == 404
            assert other.delete(f"/api/albums/{album['id']}").status_code == 404
            # 也不能把别人的单集塞进自己的专辑
            mine = other.post("/api/albums", json={"title": "我的"}).json()
            response = other.post(
                f"/api/albums/{mine['id']}/episodes",
                json={"episode_ids": [episode["id"]]},
            )
            assert response.status_code == 200
            assert response.json()["episode_count"] == 0

    def test_episode_filter_by_album(self, client):
        a = make_episode(client)
        b = make_episode(client)
        album = client.post("/api/albums", json={"title": "T"}).json()
        client.post(f"/api/albums/{album['id']}/episodes", json={"episode_ids": [a["id"]]})

        in_album = client.get(f"/api/episodes?album={album['id']}").json()
        assert [e["id"] for e in in_album["items"]] == [a["id"]]
        outside = client.get("/api/episodes?album=none").json()
        assert [e["id"] for e in outside["items"]] == [b["id"]]
        # 用别人的专辑 id 过滤要 404，不能静默返回空列表假装「这辑是空的」
        assert client.get("/api/episodes?album=al_nope").status_code == 404


# --------------------------------------------------------------------------
# 一键分享
# --------------------------------------------------------------------------


class TestShare:
    @pytest.fixture
    def owner_client(self, tmp_path):
        app = make_app(tmp_path)
        with TestClient(app) as client:
            register(client, "guo")
            yield client

    def test_share_then_anon_can_read(self, owner_client, tmp_path):
        episode = make_episode(owner_client)
        assert episode["visibility"] == "private"
        assert episode["share_token"] is None

        shared = owner_client.post(f"/api/episodes/{episode['id']}/share").json()
        token = shared["share_token"]
        assert shared["visibility"] == "public" and token

        with as_user(tmp_path) as (anon, _):
            view = anon.get(f"/api/share/{token}")
            assert view.status_code == 200
            body = view.json()
            assert body["title"] == episode["title"]
            assert body["audio_url"].startswith(f"/api/share/{token}/")
            assert body["script"]["segments"]
            # 公开视图不能泄漏内部结构
            for leaked in ("id", "options", "source_ref", "raw_text", "error", "user_id"):
                assert leaked not in body, f"公开视图泄漏了 {leaked}"
            assert "username" not in (body.get("author") or {})
            # 未登录也能放音频（Range 也要работать）
            audio = anon.get(body["audio_url"])
            assert audio.status_code in (200, 206)
            covered = anon.get(body["cover_url"])
            assert covered.status_code == 200

    def test_share_twice_keeps_the_same_link(self, owner_client):
        """点两次「分享」不能把刚发出去的链接弄失效。"""
        episode = make_episode(owner_client)
        first = owner_client.post(f"/api/episodes/{episode['id']}/share").json()
        second = owner_client.post(f"/api/episodes/{episode['id']}/share").json()
        assert first["share_token"] == second["share_token"]

    def test_unshare_kills_the_link(self, owner_client, tmp_path):
        episode = make_episode(owner_client)
        token = owner_client.post(f"/api/episodes/{episode['id']}/share").json()["share_token"]
        response = owner_client.delete(f"/api/episodes/{episode['id']}/share").json()
        assert response["visibility"] == "private" and response["share_token"] is None

        with as_user(tmp_path) as (anon, _):
            assert anon.get(f"/api/share/{token}").status_code == 404
            assert anon.get(f"/api/share/{token}/audio").status_code == 404

    def test_reset_changes_token(self, owner_client, tmp_path):
        episode = make_episode(owner_client)
        old = owner_client.post(f"/api/episodes/{episode['id']}/share").json()["share_token"]
        new = owner_client.post(f"/api/episodes/{episode['id']}/share/reset").json()[
            "share_token"
        ]
        assert new and new != old
        with as_user(tmp_path) as (anon, _):
            assert anon.get(f"/api/share/{old}").status_code == 404
            assert anon.get(f"/api/share/{new}").status_code == 200

    def test_private_episode_is_not_shared_by_default(self, owner_client, tmp_path):
        episode = make_episode(owner_client)
        # 猜 token 猜不到，但拿一个随机串也必须 404
        with as_user(tmp_path) as (anon, _):
            assert anon.get("/api/share/not-a-real-token").status_code == 404
        assert episode["visibility"] == "private"

    def test_cannot_share_someone_elses_episode(self, tmp_path):
        with as_user(tmp_path, "guo") as (owner, _):
            episode = make_episode(owner)
        with as_user(tmp_path, "intruder") as (other, _):
            assert other.post(f"/api/episodes/{episode['id']}/share").status_code == 404

    def test_share_view_supports_language_switch(self, tmp_path):
        with TestClient(make_app(tmp_path, languages="zh,en")) as client:
            register(client, "guo")
            episode = make_episode(client)
            token = client.post(f"/api/episodes/{episode['id']}/share").json()["share_token"]
            from app.config import Settings as _S  # noqa: F401  (保持导入语义清晰)

            with as_user(tmp_path) as (anon, _):
                body = anon.get(f"/api/share/{token}").json()
                assert body["languages"] == ["zh", "en"]
                assert "?lang=en" in body["versions"]["en"]["audio_url"]
                assert anon.get(f"/api/share/{token}/audio?lang=en").status_code in (200, 206)


# --------------------------------------------------------------------------
# 首页展示
# --------------------------------------------------------------------------


class TestShowcase:
    def test_owner_sees_own_episode(self, tmp_path):
        with TestClient(make_app(tmp_path)) as client:
            register(client, "guo")
            episode = make_episode(client)
            body = client.get("/api/showcase")
            assert body.status_code == 200
            # 自己的东西走受保护地址，不带 share token
            assert body.json()["token"] is None
            assert f"/api/episodes/{episode['id']}" in body.json()["audio_url"]

    def test_anon_sees_only_public(self, tmp_path):
        with as_user(tmp_path, "guo") as (owner, _):
            episode = make_episode(owner)
            with as_user(tmp_path) as (anon, _):
                assert anon.get("/api/showcase").status_code == 404
            token = owner.post(f"/api/episodes/{episode['id']}/share").json()["share_token"]
        with as_user(tmp_path) as (anon, _):
            body = anon.get("/api/showcase").json()
            assert body["token"] == token
            assert f"/api/share/{token}" in body["audio_url"]

    def test_empty_library_404(self, tmp_path):
        with TestClient(make_app(tmp_path)) as client:
            register(client, "guo")
            assert client.get("/api/showcase").status_code == 404


# --------------------------------------------------------------------------
# 批量生成
# --------------------------------------------------------------------------


class TestBatch:
    @pytest.fixture
    def client(self, tmp_path):
        with TestClient(make_app(tmp_path)) as test_client:
            register(test_client, "guo")
            yield test_client

    def test_batch_text(self, client):
        response = client.post(
            "/api/episodes/batch",
            json={
                "source_type": "text",
                "texts": [SAMPLE_TEXT, SAMPLE_TEXT + " Extra sentence about attention."],
                "options": {"duration_min": 3, "level": "intro"},
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["total"] == 2
        assert len(body["created"]) == 2
        assert body["failed"] == []
        assert all(item["status"] == "queued" for item in body["created"])

    def test_partial_failure_does_not_block_the_rest(self, client):
        """一个链接写错不能让整批白等 —— 这是批量最要紧的性质。"""
        response = client.post(
            "/api/episodes/batch",
            json={
                "source_type": "url",
                "urls": ["not-a-url", "ftp://nope.example.com/x.pdf"],
                "options": {"duration_min": 3},
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["created"] == []
        assert len(body["failed"]) == 2
        assert all(item["reason"] for item in body["failed"])
        assert body["total"] == 2

    def test_mixed_batch(self, client):
        response = client.post(
            "/api/episodes/batch",
            json={
                "source_type": "text",
                "texts": [SAMPLE_TEXT, "too short"],
                "options": {"duration_min": 3},
            },
        )
        body = response.json()
        assert len(body["created"]) == 1
        assert len(body["failed"]) == 1
        assert "太短" in body["failed"][0]["reason"]

    def test_rejects_too_many(self, tmp_path):
        with TestClient(make_app(tmp_path, max_batch_size=2)) as client:
            register(client, "guo")
            response = client.post(
                "/api/episodes/batch",
                json={
                    "source_type": "text",
                    "texts": [SAMPLE_TEXT] * 3,
                    "options": {"duration_min": 3},
                },
            )
            assert response.status_code == 400
            assert "最多" in response.json()["detail"]

    def test_empty_list_rejected(self, client):
        assert client.post(
            "/api/episodes/batch",
            json={"source_type": "text", "texts": [], "options": {"duration_min": 3}},
        ).status_code == 400

    def test_missing_key_rejected(self, client):
        response = client.post(
            "/api/episodes/batch", json={"source_type": "url", "options": {"duration_min": 3}}
        )
        assert response.status_code == 400
        assert "urls" in response.json()["detail"]

    def test_batch_pdf_upload(self, client, tmp_path):
        """多选 PDF：一个坏文件混在里面，好的照样入队。"""
        good = _minimal_pdf()
        response = client.post(
            "/api/episodes/batch",
            files=[
                ("files", ("good.pdf", good, "application/pdf")),
                ("files", ("broken.pdf", b"not a pdf", "application/pdf")),
            ],
            data={"duration_min": "3", "level": "intro"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert len(body["created"]) == 1
        assert len(body["failed"]) == 1
        assert "PDF" in body["failed"][0]["reason"]

    def test_batch_requires_login(self, tmp_path):
        with as_user(tmp_path, "guo"):
            pass
        with as_user(tmp_path) as (anon, _):
            response = anon.post(
                "/api/episodes/batch",
                json={"source_type": "text", "texts": [SAMPLE_TEXT]},
            )
            assert response.status_code == 401

    def test_batched_episodes_belong_to_me(self, client):
        client.post(
            "/api/episodes/batch",
            json={
                "source_type": "text",
                "texts": [SAMPLE_TEXT],
                "options": {"duration_min": 3},
            },
        )
        assert client.get("/api/episodes").json()["total"] == 1


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def response_text(client: TestClient) -> str:
    return client.get("/api/episodes").text


def _minimal_pdf() -> bytes:
    """能被 pypdf 打开的最小 PDF。批量上传用例只需要它通过文件头与解析。"""
    return b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""


class TestChangePassword:
    def test_change_password_invalidates_other_sessions(self, tmp_path):
        """口令改了，别处挂着的旧会话必须失效 —— 否则「改密码」是假的。"""
        with as_user(tmp_path, "guo") as (client, _):
            # 在另一个「设备」上登录
            with TestClient(make_app(tmp_path)) as second_device:
                login = second_device.post(
                    "/api/auth/login", json={"username": "guo", "password": PASSWORD}
                )
                assert login.status_code == 200
                assert second_device.get("/api/auth/me").status_code == 200

                # 当前设备改口令
                changed = client.post(
                    "/api/auth/password",
                    json={"current_password": PASSWORD, "new_password": "brand-new-pass"},
                )
                assert changed.status_code == 204

                # 另一台设备的会话作废，当前这台还有效
                assert second_device.get("/api/auth/me").status_code == 401
                assert client.get("/api/auth/me").status_code == 200

            # 新口令能登录，旧口令不能
            with TestClient(make_app(tmp_path)) as fresh:
                assert fresh.post(
                    "/api/auth/login",
                    json={"username": "guo", "password": PASSWORD},
                ).status_code == 401
                assert fresh.post(
                    "/api/auth/login",
                    json={"username": "guo", "password": "brand-new-pass"},
                ).status_code == 200

    def test_wrong_current_password_rejected(self, tmp_path):
        with as_user(tmp_path, "guo") as (client, _):
            response = client.post(
                "/api/auth/password",
                json={"current_password": "not-my-password", "new_password": "brand-new-pass"},
            )
            assert response.status_code == 401

    def test_short_new_password_rejected(self, tmp_path):
        with as_user(tmp_path, "guo") as (client, _):
            response = client.post(
                "/api/auth/password",
                json={"current_password": PASSWORD, "new_password": "short"},
            )
            assert response.status_code == 400

    def test_requires_login(self, tmp_path):
        with as_user(tmp_path, "guo"):
            pass
        with as_user(tmp_path) as (anon, _):
            response = anon.post(
                "/api/auth/password",
                json={"current_password": PASSWORD, "new_password": "brand-new-pass"},
            )
            assert response.status_code == 401


class TestLoginThrottle:
    """登录限流：没有它，登录接口就是可以无限次尝试的口令爆破入口。"""

    def test_locks_out_after_repeated_failures(self, tmp_path):
        with TestClient(make_app(tmp_path, login_max_attempts=3)) as client:
            register(client, "guo")
            client.post("/api/auth/logout")

            for _ in range(3):
                bad = client.post(
                    "/api/auth/login", json={"username": "guo", "password": "nope"}
                )
                assert bad.status_code == 401

            blocked = client.post(
                "/api/auth/login", json={"username": "guo", "password": "nope"}
            )
            assert blocked.status_code == 429
            assert "Retry-After" in blocked.headers
            # 锁住之后**正确口令也进不来** —— 否则限流形同虚设
            still = client.post(
                "/api/auth/login", json={"username": "guo", "password": PASSWORD}
            )
            assert still.status_code == 429

    def test_successful_login_resets_the_counter(self, tmp_path):
        with TestClient(make_app(tmp_path, login_max_attempts=3)) as client:
            register(client, "guo")
            client.post("/api/auth/logout")
            for _ in range(2):
                client.post("/api/auth/login", json={"username": "guo", "password": "nope"})
            # 一次成功就把记录清掉
            assert client.post(
                "/api/auth/login", json={"username": "guo", "password": PASSWORD}
            ).status_code == 200
            client.post("/api/auth/logout")
            for _ in range(2):
                assert client.post(
                    "/api/auth/login", json={"username": "guo", "password": "nope"}
                ).status_code == 401
            # 计数被清零，所以还没到阈值
            assert client.post(
                "/api/auth/login", json={"username": "guo", "password": "nope"}
            ).status_code == 401

    def test_throttle_is_per_username_and_source(self, tmp_path):
        """锁住的只是「这个用户名 + 这个来源」，不能把别人一起锁了。

        只按用户名计：别人故意打错就能把真实用户锁在门外。
        只按来源计：同一个出口 IP 后面的所有人共享额度。
        """
        throttle_max = 3
        with TestClient(make_app(tmp_path, login_max_attempts=throttle_max)) as client:
            register(client, "guo")
            client.post("/api/auth/logout")
            for _ in range(throttle_max):
                client.post("/api/auth/login", json={"username": "guo", "password": "nope"})
            assert client.post(
                "/api/auth/login", json={"username": "guo", "password": PASSWORD}
            ).status_code == 429
            # 换一个用户名不受影响（同一个来源 IP）
            assert client.post(
                "/api/auth/login", json={"username": "someone-else", "password": "nope"}
            ).status_code == 401

    def test_throttle_unit(self):
        throttle = auth_lib.LoginThrottle(max_attempts=2, lockout_minutes=15)
        key = throttle.key("guo", "10.0.0.1")
        assert throttle.retry_after(key) == 0
        throttle.record_failure(key)
        assert throttle.retry_after(key) == 0
        throttle.record_failure(key)
        assert throttle.retry_after(key) > 0
        throttle.reset(key)
        assert throttle.retry_after(key) == 0

    def test_throttle_window_expires(self):
        throttle = auth_lib.LoginThrottle(max_attempts=1, lockout_minutes=15)
        throttle.lockout_seconds = 1  # 缩短窗口，避免测试真的等 15 分钟
        key = throttle.key("guo", "10.0.0.1")
        throttle.record_failure(key)
        assert throttle.retry_after(key) > 0
        time.sleep(1.1)
        assert throttle.retry_after(key) == 0


class TestGenerationQuota:
    """生成配额：一次生成要调大模型 + 语音合成 + 视频编码，是真金白银。

    有了多账号之后，不给上限就等于把账单交给任何注册进来的人。
    """

    def test_limit_blocks_further_generation(self, tmp_path):
        with TestClient(
            make_app(tmp_path, daily_generation_limit=2)
        ) as client:
            register(client, "guo")
            make_episode(client)
            make_episode(client)
            blocked = client.post(
                "/api/episodes",
                json={
                    "source_type": "text",
                    "text": SAMPLE_TEXT,
                    "options": {"duration_min": 3},
                },
            )
            assert blocked.status_code == 429
            assert "最多生成 2 期" in blocked.json()["detail"]
            assert "X-Quota-Reset" in blocked.headers

    def test_usage_endpoint_reports_remaining(self, tmp_path):
        with TestClient(make_app(tmp_path, daily_generation_limit=3)) as client:
            register(client, "guo")
            assert client.get("/api/usage").json() == {
                "used": 0,
                "limit": 3,
                "remaining": 3,
                "resets_at": client.get("/api/usage").json()["resets_at"],
            }
            make_episode(client)
            body = client.get("/api/usage").json()
            assert body["used"] == 1 and body["remaining"] == 2

    def test_batch_counts_each_paper(self, tmp_path):
        """批量是一次提交、多篇扣额 —— 不按「提交次数」算，否则批量就是绕过配额的后门。"""
        with TestClient(make_app(tmp_path, daily_generation_limit=3)) as client:
            register(client, "guo")
            too_many = client.post(
                "/api/episodes/batch",
                json={
                    "source_type": "text",
                    "texts": [SAMPLE_TEXT] * 4,
                    "options": {"duration_min": 3},
                },
            )
            assert too_many.status_code == 429

    def test_zero_means_unlimited(self, tmp_path):
        with TestClient(make_app(tmp_path, daily_generation_limit=0)) as client:
            register(client, "guo")
            body = client.get("/api/usage").json()
            assert body["limit"] == 0 and body["remaining"] is None
            for _ in range(3):
                make_episode(client)  # 不该被拦

    def test_quota_is_per_user(self, tmp_path):
        with as_user(tmp_path, "guo", daily_generation_limit=1) as (client, _):
            make_episode(client)
            assert client.post(
                "/api/episodes",
                json={
                    "source_type": "text",
                    "text": SAMPLE_TEXT,
                    "options": {"duration_min": 3},
                },
            ).status_code == 429

        with as_user(tmp_path, "other", daily_generation_limit=1) as (other, _):
            body = other.get("/api/usage").json()
            assert body["used"] == 0 and body["remaining"] == 1

    def test_open_mode_is_not_limited(self, tmp_path):
        """开放模式（库里没账号）本来就只有部署者自己用，不该被拦。"""
        with TestClient(make_app(tmp_path, daily_generation_limit=1)) as client:
            make_episode(client)
            make_episode(client)

    def test_usage_requires_login(self, tmp_path):
        with as_user(tmp_path, "guo"):
            pass
        with as_user(tmp_path) as (anon, _):
            assert anon.get("/api/usage").status_code == 401


class TestDeleteCleansEverything:
    """删单集必须把产物删干净。

    踩过：删单集只清了**顶层字段**指向的文件 ——
      - 双语集的**非主语言**那一份（信息图 / 音频 / 视频）原地留下；
      - `video-frames/{id}/` 整个渲染中间目录没人管（每帧 SVG + 段落配图）。
    实测库里只剩 4 集，`data/video-frames/` 下却躺着十几个已删单集的目录。
    """

    def test_delete_removes_all_language_assets_and_workdir(self, tmp_path):
        """造一个假的双语集（产物文件自己写），删掉后所有文件与工作目录都该消失。"""
        from app.services.video import compose_video  # noqa: F401  (确保模块可导入)

        app = make_app(tmp_path)
        settings = app.state.settings if False else None  # noqa: F841

        with TestClient(app) as client:
            register(client, "guo")
            episode = make_episode(client)
            episode_id = episode["id"]

            data_dir = tmp_path / "data"
            work_dir = data_dir / "video-frames" / episode_id
            work_dir.mkdir(parents=True, exist_ok=True)
            frame = work_dir / "frame-000.svg"
            frame.write_text("<svg/>", encoding="utf-8")

            # 造一份非主语言的产物，模拟双语集
            extra_illustration = data_dir / "illustrations" / f"{episode_id}.en.png"
            extra_illustration.parent.mkdir(parents=True, exist_ok=True)
            extra_illustration.write_bytes(b"png")
            extra_audio = data_dir / "audio" / f"{episode_id}.en.mp3"
            extra_audio.parent.mkdir(parents=True, exist_ok=True)
            extra_audio.write_bytes(b"mp3")
            topic = work_dir / "topics" / f"{episode_id}.en-topic1.png"
            topic.parent.mkdir(parents=True, exist_ok=True)
            topic.write_bytes(b"png")

            db = client.app.state.db
            record = db.get_episode(episode_id)
            versions = dict(record.get("versions") or {})
            versions["en"] = {
                "language": "en",
                "audio_path": str(extra_audio),
                "illustration": {
                    "png_path": str(extra_illustration),
                    "svg_path": None,
                    "width": 1,
                    "height": 1,
                    "source": "model",
                },
                "video": {"assets": {"topic1": str(topic)}},
            }
            db.update_episode(episode_id, versions=versions)

            assert extra_audio.exists() and extra_illustration.exists() and work_dir.exists()

            assert client.delete(f"/api/episodes/{episode_id}").status_code == 204

            assert not extra_audio.exists(), "非主语言的音频没删掉"
            assert not extra_illustration.exists(), "非主语言的信息图没删掉"
            assert not topic.exists(), "视频用的段落配图没删掉"
            assert not work_dir.exists(), "video-frames 工作目录没删掉"
