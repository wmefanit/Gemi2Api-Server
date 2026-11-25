"""同请求合并与答案重放。"""

import asyncio

import request_cache as rc


def test_fingerprint_ignores_volatile_fields():
	base = {"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "hi"}]}
	a = rc.fingerprint({**base, "stream": False, "user": "abc"})
	b = rc.fingerprint({**base, "stream": True, "user": "zzz"})
	assert a == b


def test_fingerprint_changes_with_content():
	a = rc.fingerprint({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
	b = rc.fingerprint({"model": "m", "messages": [{"role": "user", "content": "hi2"}]})
	assert a != b


def test_replay_within_window_only():
	now = [1000.0]
	registry = rc.RequestRegistry(replay_ttl=60, clock=lambda: now[0])
	registry.remember("k", {"ok": True})
	assert registry.replay("k") == {"ok": True}
	now[0] += 61
	assert registry.replay("k") is None


def test_replay_evicts_oldest_when_full():
	now = [0.0]
	registry = rc.RequestRegistry(max_entries=2, clock=lambda: now[0])
	registry.remember("a", 1)
	now[0] += 1
	registry.remember("b", 2)
	now[0] += 1
	registry.remember("c", 3)
	assert registry.replay("a") is None
	assert registry.replay("b") == 2 and registry.replay("c") == 3


def test_coalesce_runs_once_for_concurrent_calls():
	calls = []

	async def factory():
		calls.append(1)
		await asyncio.sleep(0.05)
		return "answer"

	async def main():
		registry = rc.RequestRegistry()
		results = await asyncio.gather(*(registry.coalesce("k", factory) for _ in range(4)))
		return registry, results

	registry, results = asyncio.run(main())
	assert len(calls) == 1
	assert [value for value, _joined in results] == ["answer"] * 4
	assert sum(1 for _v, joined in results if joined) == 3
	assert registry.joined_count == 3


def test_coalesce_propagates_failure_and_clears_inflight():
	async def main():
		registry = rc.RequestRegistry()

		async def boom():
			await asyncio.sleep(0.01)
			raise RuntimeError("upstream down")

		outcomes = await asyncio.gather(*(registry.coalesce("k", boom) for _ in range(2)), return_exceptions=True)
		# 失败之后同一个 key 还能重新跑
		async def ok():
			return "fresh"

		return registry, outcomes, await registry.coalesce("k", ok)

	registry, outcomes, retry = asyncio.run(main())
	assert all(isinstance(item, RuntimeError) for item in outcomes)
	assert retry == ("fresh", False)
	assert registry.joined_count == 1
