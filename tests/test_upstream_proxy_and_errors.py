"""离线回归测试：服务池必须使用配置出口，瞬时上游故障不能伪装成 500。"""

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

import main
from cookie_pool import CookieEntry, TemporarilyBlockedError


def test_get_gemini_pool_passes_configured_proxy(monkeypatch):
	monkeypatch.setattr(main, "gemini_pool", None)
	monkeypatch.setattr(main, "load_cookie_entries", lambda **_kwargs: [CookieEntry("psid-test", "ts-test", "test")])
	monkeypatch.setattr(main, "load_extra_cookies", lambda: {})
	monkeypatch.setattr(main, "GEMINI_PROXY", "socks5h://proxy.example:1080")

	captured = {}

	def fake_pool(entries, **kwargs):
		captured["entries"] = entries
		captured.update(kwargs)
		return SimpleNamespace()

	monkeypatch.setattr(main, "CookiePool", fake_pool)
	pool = asyncio.run(main.get_gemini_pool())

	assert pool is main.gemini_pool
	assert captured["proxy"] == "socks5h://proxy.example:1080"


def test_live_quota_client_uses_the_configured_proxy(monkeypatch):
	monkeypatch.setattr(main, "SECURE_1PSID", "psid-test")
	monkeypatch.setattr(main, "SECURE_1PSIDTS", "psidts-test")
	monkeypatch.setattr(main, "GEMINI_PROXY", "socks5h://proxy.example:1080")
	monkeypatch.setattr(main, "LAST_QUOTA", None)

	captured = {}

	class FakeClient:
		def __init__(self, _psid, _psidts, **kwargs):
			captured.update(kwargs)
			self.cookies = {}

		async def init(self, **_kwargs):
			return None

		async def close(self):
			return None

	monkeypatch.setattr(main, "GeminiClient", FakeClient)
	asyncio.run(main.fetch_live_quota())

	assert captured["proxy"] == "socks5h://proxy.example:1080"


def test_temporarily_blocked_upstream_returns_retryable_503(monkeypatch):
	monkeypatch.setattr(main, "API_KEY", "")
	monkeypatch.setattr(main, "_init_gemini_client_background", _noop_background_init)

	async def fake_get_pool():
		return SimpleNamespace()

	async def fake_generate(*_args, **_kwargs):
		raise TemporarilyBlockedError("Google temporarily blocked this request")

	monkeypatch.setattr(main, "get_gemini_pool", fake_get_pool)
	monkeypatch.setattr(main, "generate_with_cookie_pool", fake_generate)

	with TestClient(main.app) as client:
		response = client.post(
			"/v1/chat/completions",
			json={"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "offline test"}]},
		)

	assert response.status_code == 503
	assert response.headers["retry-after"] == "30"
	assert "上游暂时不可用" in response.json()["detail"]


async def _noop_background_init():
	return None
