"""CookiePool._ensure_client 的 init 超时兜底（离线，不访问网络）。

gemini_webapi 的 client.init 本身不受 timeout 约束，卡住时会无限等待；
这里用一个永远挂起的假客户端，确认外层 asyncio.wait_for 能把它打断，
按初始化失败处理（关闭客户端、抛 CookiePoolUnavailable），而不是让整个池卡死。
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import cookie_pool
from cookie_pool import CookieEntry, CookiePool, CookiePoolUnavailable


class _HangingClient:
	instances = []

	def __init__(self, psid, psidts, *args, **kwargs):
		self.psid = psid
		self.psidts = psidts
		self.closed = False
		_HangingClient.instances.append(self)

	async def init(self, timeout=None, **kwargs):
		await asyncio.Event().wait()  # 永不返回

	async def close(self, *args, **kwargs):
		self.closed = True


class _OkClient(_HangingClient):
	async def init(self, timeout=None, **kwargs):
		return None


@pytest.fixture(autouse=True)
def _reset():
	_HangingClient.instances.clear()
	yield
	_HangingClient.instances.clear()


def test_hanging_init_times_out(monkeypatch):
	monkeypatch.setattr(cookie_pool, "GeminiClient", _HangingClient)
	validated = []

	async def validator(client, tag):
		validated.append(tag)

	pool = CookiePool([CookieEntry("psid-a", "ts-a", "a")], validator=validator, init_timeout=0.2)

	async def run():
		start = time.monotonic()
		with pytest.raises(CookiePoolUnavailable) as info:
			await pool._ensure_client(pool.slots[0])
		return time.monotonic() - start, info.value

	elapsed, err = asyncio.run(asyncio.wait_for(run(), timeout=5))
	assert elapsed < 2, f"超时没生效，耗时 {elapsed:.2f}s"
	assert "超过" in str(err)
	assert isinstance(err.__cause__, TimeoutError)
	assert validated == []
	assert pool.slots[0].client is None
	assert _HangingClient.instances and all(c.closed for c in _HangingClient.instances)


def test_acquire_does_not_hang_and_marks_slot(monkeypatch):
	monkeypatch.setattr(cookie_pool, "GeminiClient", _HangingClient)
	pool = CookiePool([CookieEntry("psid-a", "ts-a", "a")], init_timeout=0.2)

	async def run():
		with pytest.raises(CookiePoolUnavailable):
			await pool.acquire()

	asyncio.run(asyncio.wait_for(run(), timeout=5))
	assert pool.slots[0].failures >= 1
	assert pool.slots[0].cooldown_until > time.time()


def test_normal_init_unaffected(monkeypatch):
	monkeypatch.setattr(cookie_pool, "GeminiClient", _OkClient)
	pool = CookiePool([CookieEntry("psid-a", "ts-a", "a")], init_timeout=0.2)

	client = asyncio.run(pool._ensure_client(pool.slots[0]))
	assert isinstance(client, _OkClient)
	assert pool.slots[0].client is client
	assert not client.closed
