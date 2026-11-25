"""让 gemini-webapi 能在 SOCKS 代理后面工作。

gemini_webapi 在取 access token / 下载图片时写死了 ``allow_redirects=CurlFollow.SAFE``。
SAFE 是 curl_cffi 的 SSRF 防护：跟随重定向时，如果实际连上的地址是内网地址就拒绝。走代理时
「实际连上的地址」是代理本身；代理一旦部署在内网（例如 ub.host:1083 解析到 docker 网桥里的
172.17.0.11），任何一次跨主机跳转都会被判成 SSRF，不跳转的请求则不受影响：

    curl: (7) Redirect to internal IP 172.17.0.11 rejected (SSRF protection)

这不是代理配置错误，本地端口转发也救不了（连上的仍是私网地址）；SAFE 只是一个开关，不能给代理
地址开白名单，只能在进程内把 SAFE 放宽成
普通跟随重定向。仅在配置了代理时才放宽，没配代理时保留库的默认防护。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 库里写死 CurlFollow.SAFE 的模块
_PATCHED_MODULES = (
	"gemini_webapi.utils.get_access_token",
	"gemini_webapi.types.image",
	"gemini_webapi.types.video",
)


class _FollowShim:
	"""替身：只把 SAFE 换成普通跟随，其余取值保持库里原有语义。"""

	ALL = 1
	OBEYCODE = 2
	FIRSTONLY = 3
	SAFE = True


def relax_safe_redirects() -> int:
	"""把库里的 CurlFollow.SAFE 换成普通跟随重定向，返回改写过的模块数。"""
	patched = 0
	for module_name in _PATCHED_MODULES:
		try:
			module: Any = importlib.import_module(module_name)
		except Exception as exc:  # 库版本不同、模块不存在都不该让服务起不来
			logger.debug("跳过 %s：%s", module_name, exc)
			continue
		current = getattr(module, "CurlFollow", None)
		if current is None or getattr(current, "SAFE", None) is True:
			continue
		module.CurlFollow = _FollowShim
		patched += 1
	return patched
