"""proxy_compat：内网代理下放宽 gemini_webapi 写死的 CurlFollow.SAFE。

离线用例只检查模块替换是否正确；联网用例默认跳过，设置 G2A_LIVE_PROXY（如 socks5h://ub.host:1083）
后才跑，只访问 github，不访问 Google。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from curl_cffi import CurlFollow

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import proxy_compat  # noqa: E402


@pytest.fixture
def pristine_modules(monkeypatch):
	"""每个用例开始前把三个模块的 CurlFollow 还原成真枚举，用例结束后自动恢复。"""
	modules = []
	for name in proxy_compat._PATCHED_MODULES:
		module = importlib.import_module(name)
		monkeypatch.setattr(module, "CurlFollow", CurlFollow)
		modules.append(module)
	return modules


def test_relax_replaces_safe_in_all_modules(pristine_modules):
	assert proxy_compat.relax_safe_redirects() == len(pristine_modules)
	for module in pristine_modules:
		assert module.CurlFollow is proxy_compat._FollowShim


def test_relax_is_idempotent(pristine_modules):
	proxy_compat.relax_safe_redirects()
	assert proxy_compat.relax_safe_redirects() == 0


def test_shim_keeps_other_values_and_maps_safe_to_follow_all():
	shim = proxy_compat._FollowShim
	assert int(shim.SAFE) == int(CurlFollow.ALL)
	assert shim.ALL == int(CurlFollow.ALL)
	assert shim.OBEYCODE == int(CurlFollow.OBEYCODE)
	assert shim.FIRSTONLY == int(CurlFollow.FIRSTONLY)


LIVE_PROXY = os.environ.get("G2A_LIVE_PROXY")


@pytest.mark.skipif(not LIVE_PROXY, reason="未设置 G2A_LIVE_PROXY，跳过联网用例")
def test_live_cross_host_redirect_through_internal_proxy():
	"""复现：经内网代理跟随跨主机跳转，SAFE 报 internal IP，放宽后正常。"""
	from curl_cffi import requests as cr

	url = "https://www.github.com/"  # 301 -> https://github.com/
	with pytest.raises(Exception, match="internal IP"):
		cr.get(url, proxy=LIVE_PROXY, impersonate="chrome", allow_redirects=CurlFollow.SAFE, timeout=20)

	resp = cr.get(
		url,
		proxy=LIVE_PROXY,
		impersonate="chrome",
		allow_redirects=proxy_compat._FollowShim.SAFE,
		timeout=20,
	)
	assert resp.status_code == 200
	assert resp.url.startswith("https://github.com/")
