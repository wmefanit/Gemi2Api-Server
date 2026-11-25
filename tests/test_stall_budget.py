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


def test_ssl_init_error_does_not_cool_or_degrade_account(monkeypatch):
	"""curl 35 是出口/网络问题，不能把整个账号槽位冷却或降级。"""

	class _SslBlip(_UpstreamDefaults):
		async def init(self, timeout=None, **kwargs):
			raise cookie_pool.CurlError("curl: (35) SSL_ERROR_SYSCALL")

	monkeypatch.setattr(cookie_pool, "GeminiClient", _SslBlip)
	pool = _pool(cooldown_wait=0.0)

	async def run():
		with pytest.raises(CookiePoolUnavailable):
			await pool.acquire()

	asyncio.run(run())
	slot = pool.slots[0]
	remaining = slot.cooldown_until - time.time()
	assert slot.failures == 1
	assert slot.account_failures == 0
	assert remaining <= 0.05, remaining


def test_temporarily_blocked_1060_does_not_cool_or_degrade_account():
	pool = _pool()
	slot = pool.slots[0]
	error = cookie_pool.TemporarilyBlockedError("Google temporarily blocked this request")

	for _ in range(pool.degrade_after + 2):
		pool.report_failure(slot, error)

	assert slot.failures == pool.degrade_after + 2
	assert slot.account_failures == 0
	assert slot.cooldown_until <= time.time()
	assert pool._pick(set()) is slot


def test_wrapped_ssl_handshake_error_is_transient_but_other_curl_errors_are_not():
	ssl_error = cookie_pool.CurlSSLError("curl: (35) SSL_connect closed")
	wrapped = RuntimeError("upstream request failed")
	wrapped.__cause__ = ssl_error
	assert cookie_pool.is_transient_network_failure(wrapped)

	timeout = cookie_pool.CurlError("curl: (28) operation timed out")
	assert not cookie_pool.is_transient_network_failure(timeout)
	assert cookie_pool.is_transient_network_failure(
		RuntimeError("Your IP address has been temporarily flagged or blocked by Google")
	)
	assert cookie_pool.is_transient_network_failure(
		RuntimeError("curl: (35) Connection closed abruptly by the remote SSL endpoint")
	)


def test_transient_generation_failure_discards_client_but_allows_immediate_retry():
	pool = _pool()

	async def run():
		lease = await pool.acquire()
		failed_client = lease.client
		lease.fail(cookie_pool.TemporarilyBlockedError("Google error 1060"))
		lease.release()
		assert pool.slots[0].client is None
		assert pool.slots[0].cooldown_until <= time.time()
		retry = await pool.acquire()
		retry_client = retry.client
		retry.release()
		await asyncio.sleep(0)
		return failed_client, retry_client

	failed_client, retry_client = asyncio.run(run())
	assert failed_client is not retry_client
	assert pool.slots[0].account_failures == 0


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
