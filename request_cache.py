"""同请求合并与答案重放。

客户端重试风暴（断流、反代超时、SDK 自带重试）会把同一份 prompt 连打好几次，
每次都真实打一次上游：浪费额度，还会因为并发使用同一个 1PSIDTS 触发 Google 吊销会话
（本地实测过这种吊销，代价是整个账号要重新登录取 cookie）。

这里做两件事：

1. **在途合并**：同一指纹的请求只放一个到上游，其余等它的结果。客户端拿到的是同一份
   答案，不会因为并发重复消耗账号。
2. **答案重放**：非流式答案在窗口内直接复用。重试拿到的就是上一次的答案，不再打上游。

流式请求不参与：把流攒起来再分发给多个客户端得缓冲整个响应，代价比收益大。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

#: 重放窗口（秒）
REPLAY_TTL_SECONDS = 15 * 60
#: 重放缓存条数上限，超出按写入时间淘汰最旧的
MAX_REPLAY_ENTRIES = 256
#: 计算指纹时忽略的易变字段：它们不影响模型输出，却会让同一问题算成两个指纹
VORLATILE_KEYS = ("stream", "user", "metadata", "request_id", "store", "extra_body")


def fingerprint(payload: Dict[str, Any]) -> str:
	"""按请求内容算指纹：同一个问题（同样的历史/工具/采样参数）得到同一个值。"""
	normalized = {key: value for key, value in payload.items() if key not in VORLATILE_KEYS}
	blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
	return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class RequestRegistry:
	"""在途请求合并 + 非流式答案重放。"""

	def __init__(
		self,
		replay_ttl: float = REPLAY_TTL_SECONDS,
		max_entries: int = MAX_REPLAY_ENTRIES,
		clock: Callable[[], float] = time.monotonic,
	) -> None:
		self._replay_ttl = replay_ttl
		self._max_entries = max_entries
		self._clock = clock
		self._inflight: Dict[str, asyncio.Future] = {}
		self._answers: Dict[str, Tuple[float, Any]] = {}
		self._joined = 0

	@property
	def joined_count(self) -> int:
		"""有多少请求搭了别人的车（日志/自检用）。"""
		return self._joined

	def reset(self) -> None:
		self._inflight.clear()
		self._answers.clear()
		self._joined = 0

	# -- 答案重放 ---------------------------------------------------------
	def replay(self, key: str) -> Optional[Any]:
		entry = self._answers.get(key)
		if entry is None:
			return None
		stored_at, value = entry
		if self._clock() - stored_at > self._replay_ttl:
			self._answers.pop(key, None)
			return None
		return value

	def remember(self, key: str, value: Any) -> None:
		now = self._clock()
		# 顺手清掉过期项，避免长时间运行后字典只增不减
		for stale in [key_ for key_, (at, _v) in self._answers.items() if now - at > self._replay_ttl]:
			self._answers.pop(stale, None)
		if len(self._answers) >= self._max_entries:
			oldest = min(self._answers, key=lambda key_: self._answers[key_][0])
			self._answers.pop(oldest, None)
		self._answers[key] = (now, value)

	# -- 在途合并 ---------------------------------------------------------
	async def coalesce(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Tuple[Any, bool]:
		"""同指纹只跑一次 factory。返回 (结果, 是否搭车)。"""
		inflight = self._inflight.get(key)
		if inflight is not None:
			self._joined += 1
			return await asyncio.shield(inflight), True

		loop = asyncio.get_running_loop()
		future: asyncio.Future = loop.create_future()
		# 没有搭车者时异常不会被取走，加个回调读一次，免得事件循环报 "exception never retrieved"
		future.add_done_callback(lambda done: done.cancelled() or done.exception())
		self._inflight[key] = future
		try:
			value = await factory()
		except BaseException as exc:
			self._inflight.pop(key, None)
			if not future.done():
				future.set_exception(exc)
			raise
		self._inflight.pop(key, None)
		if not future.done():
			future.set_result(value)
		return value, False
