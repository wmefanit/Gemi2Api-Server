"""上游卡死预算与冷却等待（离线）。

起因：客户端并发时 Gemini 会长时间不吐字，gemini_webapi 的 recovery 轮询上限
取 min(timeout, watchdog_timeout)=120s，一个请求就占住账号两分钟，随后账号进
冷却，同一账号上的其它请求全部 503。客户端不会为此降速，所以服务端必须自己
把等待上限压小，并在冷却期内等待而不是立刻拒绝。
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import cookie_pool
from cookie_pool import CookieEntry, CookiePool, CookiePoolUnavailable


class _UpstreamDefaults:
	"""默认值照抄 gemini_webapi：timeout=450、watchdog_timeout=120。"""

	def __init__(self, psid, psidts, *args, **kwargs):
		self.timeout = 450.0
		self.watchdog_timeout = 120.0
		self.closed = False

	async def init(self, timeout=None, **kwargs):
		return None

	async def close(self, *args, **kwargs):
		self.closed = True


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
	monkeypatch.setattr(cookie_pool, "GeminiClient", _UpstreamDefaults)


def _pool(**kwargs):
	return CookiePool([CookieEntry("psid-a", "ts-a", "a")], **kwargs)


def test_stall_budget_shrinks_upstream_timeouts():
	pool = _pool(stall_timeout=60.0)

	async def run():
		lease = await pool.acquire()
		lease.release()
		return lease.client

	client = asyncio.run(run())
	assert client.timeout == 60.0
	assert client.watchdog_timeout == 60.0


def test_stall_budget_never_raises_existing_smaller_values():
	pool = _pool(stall_timeout=60.0)

	async def run():
		lease = await pool.acquire()
		client = lease.client
		client.timeout = 0.0  # 0 表示不限
		lease.release()
		return client

	client = asyncio.run(run())
	assert client.timeout == 0.0


def test_acquire_waits_for_short_cooldown():
	pool = _pool(cooldown_wait=1.0)

	async def run():
		pool.slots[0].cooldown_until = time.time() + 0.25
		start = time.monotonic()
		lease = await pool.acquire()
		elapsed = time.monotonic() - start
		lease.release()
		return elapsed

	elapsed = asyncio.run(run())
	assert 0.2 <= elapsed < 1.0, elapsed


def test_acquire_fails_fast_when_cooldown_longer_than_wait():
	pool = _pool(cooldown_wait=0.3)

	async def run():
		pool.slots[0].cooldown_until = time.time() + 60
		start = time.monotonic()
		with pytest.raises(CookiePoolUnavailable):
			await pool.acquire()
		return time.monotonic() - start

	elapsed = asyncio.run(run())
	assert elapsed < 0.3, elapsed


def test_transient_init_error_uses_short_cooldown(monkeypatch):
	"""握手抖动（curl 35）不该把账号冻 300s——之前 permanent 一律 True 就是这样。"""

	class _SslBlip(_UpstreamDefaults):
		async def init(self, timeout=None, **kwargs):
			raise RuntimeError(
				"Failed to perform, curl: (35) BoringSSL SSL_connect: Connection closed abruptly"
			)

	monkeypatch.setattr(cookie_pool, "GeminiClient", _SslBlip)
	pool = _pool(cooldown_wait=0.0)

	async def run():
		with pytest.raises(CookiePoolUnavailable):
			await pool.acquire()

	asyncio.run(run())
	slot = pool.slots[0]
	remaining = slot.cooldown_until - time.time()
	assert slot.failures == 1
	assert remaining <= pool.cooldown_base + 1, remaining


def test_auth_failure_still_gets_long_cooldown(monkeypatch):
	class _AuthDead(_UpstreamDefaults):
		async def init(self, timeout=None, **kwargs):
			raise RuntimeError("Error: Authentication failed (UNAUTHENTICATED)")

	monkeypatch.setattr(cookie_pool, "GeminiClient", _AuthDead)
	pool = _pool(cooldown_wait=0.0)

	async def run():
		with pytest.raises(CookiePoolUnavailable):
			await pool.acquire()

	asyncio.run(run())
	slot = pool.slots[0]
	assert slot.cooldown_until - time.time() > 60
