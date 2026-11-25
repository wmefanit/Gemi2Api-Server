# 分支与提交对照（integration/all 历史重写后的溯源表）

2026-11-24 把 `integration/all` 的历史从 33 个提交压成 6 个。原因：同一份内容被反复 amend + merge 成多个重复提交（"删关键词匹配"出现过 6 次），中间还夹着"加守卫 → 删守卫"的来回，没人能读懂。

**内容一字未改**，只多了一条 `.gitignore`（忽略 `run.log`、`.env.bak*`）。

- 旧 head：`83acc9a`，保留在分支 `backup/all-before-cleanup` 与 tag `backup-all-*`
- 新 head：`4b1a25c`

## 新提交 ← 吸收了哪些旧分支

| 新提交 | 内容 | 来源分支（尖端） |
|---|---|---|
| `318744c` feat(tools): 工具调用协议注入与拒答治理 | `gemini_tools.py` 契约文案、围栏解析、场景脚本 | `feat/tool-contract`（`23d59c4`）、`feat/contract-retry`（`3b9d9cb`，其历史里先加后删的守卫已被这次重写丢弃） |
| `43a8a44` feat(reliability): 同请求合并、答案重放与请求 id | `request_cache.py` | `feat/reliability`（`f41a1b0`） |
| `69f7504` fix(proxy): 内网 SOCKS 代理不再被 SAFE 判成 SSRF | `proxy_compat.py` | `fix/socks-proxy`（`4013829`）、`c2cb3a4` 里的 SAFE 放宽 |
| `a28cc83` fix(pool): 出口代理、init 超时、卡死预算与冷却策略 | `cookie_pool.py` | `fix/init-timeout`（`7ccecfb`）、`fix/stall-budget`（`6ed14f3`）、`c2cb3a4` 的 proxy 参数 |
| `1be7851` feat(app): main 接线 | `main.py` | 上述各分支在 `main.py` 里的接线（缓存/契约/代理/超时开关） |
| `4b1a25c` docs+build: 移植清单与 pytest 基线 | `docs/port-plan.md`、`pyproject.toml`、`.gitignore` | `chore/pytest-baseline`（`bc9887a`）、`chore/task-download-probe`（`0484d33`） |

## 使用规则

- **老分支只作档案**：`feat/*`、`fix/*`、`chore/*` 都指向旧历史，可以 `git log`、`git diff origin/main...<branch>`、cherry-pick 单个提交，但**不要再 merge 进 `integration/all`**，否则重复内容会重新出现。
- **新工作从 `integration/all` 开分支**，命名写清"解决什么现场问题"，一个分支一件事。
- **合并前先跑** `pytest tests`（37 项，含工具协议、缓存、超时、冷却、代理）。
- 想回看"加守卫再删守卫"那段弯路：`git log backup/all-before-cleanup`。

## 这段弯路本身（留档，免得后人重犯）

1. 用正则猜"模型在拒答"→ 改 502 + 补问一轮。实测：换措辞即绕过，且多等一个完整往返。
2. 契约外面包 `<<<GATEWAY_TOOL_CONTRACT>>>`。实测：模型回"这不是真实工具"，反而更不调用。
3. 网络握手抖动（`curl (35)`）被当成 cookie 失效，冻账号 300 秒，表现为全站 503 五分钟。
