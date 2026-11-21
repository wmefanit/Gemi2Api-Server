# Gemi2Api-Server 改造清单（Task 优先）

本地 clone：`/mnt/data/code/Gemi2Api-Server`，HEAD `6cb8c32`（2026-11-21）。
体量：`main.py` 1736 行、`admin.py` 687、`cookie_pool.py` 627、`protocols.py` 531、`gemini_models.py` 432、`gemini_tools.py` 414、`gemini_wire.py` 205，合计约 4.7k 行。
测试：仅 `tests/selftest_tools.py`（128 行）、`tests/selftest_wire.py`（167 行），都是脚本式自检，没有 pytest。

---

## 0. Task 支持（第一优先级）

### 现状（已核对代码）
- `gemini_task.py:26 ensure_task_ready`：写账号偏好 `222`（RPC `L5adhe`）+ 补 `UVb0T` 条款同意（条款版本 0、类别 12、首页令牌）。
- `main.py:908` 起识别 `-task` 后缀；`main.py:1275` 起 Task 专用路径。
- **Task 内部强制非流式**（`main.py:1324` 注释：进度帧长时间无正文），完成后从 `response.metadata[0]` 取 cid；空正文时用 `fetch_readable_chat_response` 回读历史。
- `main.py:366 render_task_file_links`：把 `[file-tag: 文件名]` 替换成 **Gemini 会话链接**，让人工在已登录浏览器导出。
- 昨日修复 `6cb8c32`：`captured_cid` 未初始化导致流中断。

### 关键事实：自动下载被撤掉了
- `git show 0801041` 曾实现完整链路：从会话稀疏键 `"59"` 递归提取附件 → 同会话内下载 → 缓存到内存 → 暴露 `/gemini-proxy/taskfile?token=…`，失败回退 Google 直链。
- 提交 `1d7f349`（"refactor: Task 文件返回 Gemini 会话链接（轻量 fallback）"）把它换成了现在的会话链接方案，原因写在提交里：`contribution.usercontent` 下载对服务端返回 403。

**所以 Task 这块，三个候选项目都没有自动取产物的能力**：Mag1cFall 只有 40 行首用确认（`internal/gemini/task.go`），Gemi2Api 有取链路的**历史实现**但已回退。这是我们要自己补的核心。

### 计划（按顺序）
1. **恢复下载链路**：`git show 0801041 -- main.py` 取回 `_cache_task_file` / `fetch_task_attachments` / `render_task_attachments` 三段，重新接入。
2. **补鉴权与生命周期**（原实现没有）：`/gemini-proxy/taskfile?token=` 必须校验 API key（与 `/v1/*` 一致），token 加 TTL（建议 10 分钟）和一次性消费，附件加大小上限与 MIME 白名单（pdf/docx/xlsx/png/jpg）。
3. **解决 403 根因**：`contribution.usercontent` 需要浏览器会话上下文。两条路——
   - 用带完整 cookie 的请求直接打该端点（复用 `cookie_pool` 的 jar，不走匿名）；
   - 或保留"链接给人导出"作为兜底，但把链接指向**我们自己的下载代理**，由代理带 cookie 取回后回传。
   先做路径 1，403 时自动降级到会话链接（保留现有 fallback 行为）。
4. **Task 状态回读**：现在只有 `fetch_readable_chat_response` 的通用轮询。需要按 Mag1cFall `docs/protocol.md` 记录的 Task 状态帧解析（运行中/失败/产出），否则前端只能一直转圈。
5. **测试**：`ensure_task_ready` 两个 RPC 的 payload 形状（用假 client 断言）、`file-tag` 正则、token 路由鉴权、403 降级路径、`captured_cid` 回归。

---

## 1. 工具契约注入位置（我们已验证的坑）

**现状**：`gemini_tools.py:85 build_tools_instruction` 返回 `(top, bottom)`，`main.py:970-973` 把 `tools_top` 放进扁平 `parts` 列表的最前面，system 消息文本在其后。

**问题**：Gemini Web 只保留一条 system instruction，拼在普通提示文本开头的契约会被模型当成"提示词里的示例"，然后回答「我没有工具」并收尾。我们在 FastAPI 版是实测出来的：同一段文本放 user 回合 → 拒绝；作为第二条 system → 拒绝；并入首条 system → 立刻调用。

**改法**
- 在 `build_messages` 里改：找到第一条 `system` 消息，把契约**追加到它的内容末尾**，用哨兵包裹（`<<<GATEWAY_TOOL_CONTRACT>>>…<<<…>>>`）；没有 system 就新建一条放在最前。
- `tools_bottom` 保留在末尾作为二次锚定，但内容要改成"工具是真实可用的，不要转述给用户"。
- 哨兵在历史重放、会话哈希、`[file-tag]` 处理里都要剔除。

**配套**：新增 `looks_like_tool_unavailable_claim()`（中英文正则：没有/不具备/无法使用…工具、工具未注册、额度用完、"请你自行执行"），命中且本轮无 `tool_calls` 时不返回 200 文本，走换号重试。

---

## 2. 破损尾部保留可用调用

**现状**：`gemini_tools.py:192 partial_prefix_len`、`:201 tool_fence_end` 处理围栏截断，`parse_tool_calls` 解析 JSON 围栏。

**改法**：一轮里解析到 ≥1 个完整调用、尾部仍有未闭合围栏时，执行已解析调用并 `logger.warning` 记录丢弃字节数；只有 0 个可用调用才报协议错误。测试：完整+半截→1 个；仅半截→错误；两个完整+半截→2 个。

---

## 3. 单飞去重 + 已存答案重放

**现状**：无。每个请求都完整上传历史并新建会话（`main.py:1517` 才拿到 cid，只为自动删除）。

**改法**
- 新文件 `coalesce.py`：以 `(endpoint, api_key, sha256(body))` 为键；第二个相同请求等待第一个完成后重放响应体，流式则回放捕获的 SSE 帧。
- 答案重放：SQLite 建 `answers(scope, prefix_key, body, calls_json, created_at)`；`scope` 含模型+tools 声明的哈希，`prefix_key` 按消息边界累计哈希。命中即回放，区分流式/非流式两种形态。

---

## 4. 会话复用（前缀接力）

**现状**：无复用，每次新建会话。长历史每轮重传，成本高、上游慢。

**改法**：移植 FastAPI 版做法——`sessions(scope, prefix_key, cid, updated_at)`，按最长前缀精确匹配，命中后在该 cid 上续写；同一 cid 并发加进程内互斥。注意 Task 路径不能复用临时会话（`main.py:1275` 已有这条约束）。

---

## 5. 可观测性与上下文预算

- 请求号：最外层中间件读 `X-Request-Id`，缺失则生成，写进日志上下文；`admin.py` 的请求记录表加一列。
- 上下文预算：`build_messages` 组装后检查字符数，超限明确返回 4xx + `X-Context-Limit/X-Context-Used`，不静默截断。

---

## 6. 测试基线（先做这个，否则后面都悬）

- 把 `tests/selftest_*.py` 迁成 pytest（保持不访问 Google）。
- 新增 `tests/test_task.py`（任务链路）、`tests/test_tool_contract.py`、`tests/test_coalesce.py`、`tests/test_replay.py`。
- CI 加 `pytest -q`；这个仓目前没有任何自动化。

---

## 附：与另外两个候选的分工

| | 用途 |
|---|---|
| 本仓（Gemi2Api-Server） | 主改造对象：Task 历史实现、四协议、同 `gemini_webapi` 依赖 |
| Mag1cFall | 只取 `docs/protocol.md`（1637 行状态帧记录）与 Task 首用确认字段做交叉核对 |
| zexadev | 只取 `strict_model` 的换号语义和代理出口绑定的设计，不搬代码 |

---

## 附二：与 feature-inventory.md 的逐项对照

清单不是新写的，是把 `Gemini-FastAPI/docs/feature-inventory.md` 的 11 组功能逐项对照本仓代码后，只留下"本仓没有"的部分。对照结果：

| 清单条目 | 本仓现状 | 结论 |
|---|---|---|
| 1 模型后缀与稳定别名 | 已有 `-thinking/-thinking-medium/high/low`（`gemini_models.py:56`）、`-task`；无 `-del`，无本地化别名 | 只需补 `-del` 与别别名，低优先级 |
| 2 工具协议 | 有围栏与解析，但**注入位置错**、无话术拒绝、无破尾保留 | 计划 1 + 2 |
| 3 流式不降级 | 未发现静默降级路径（协议层各自实现） | 暂不动，补测试即可 |
| 4 单飞去重 | 无 | 计划 3 |
| 5 答案重放 | 无 | 计划 3 |
| 6 会话接力 | 无复用，每请求新建会话 | 计划 4 |
| 7 Cookie 缓存 | 已有 `psidts_cache` 回调（`cookie_pool.py:407`）与 `GEMINI_COOKIE_PATH` | 已具备，仅需核对轮换写回时机 |
| 8 多账号池 | 已有（在途最少优先、冷却换号） | 已具备；代理绑定参考 zexadev，不在本期 |
| 9 可观测性 | 无 request id、无上下文预算 | 计划 5 |
| 10 凭证配置 | 已用环境变量 + `secrets/` | 已具备 |
| 11 运维 | Dockerfile / compose / render.yaml 齐 | 已具备，缺 CI |

所以本仓真正的工作量集中在 2、4、5、6、9 五项 + Task 链路，不是把 11 组全部重做一遍。

---

## 附三：Task 产物下载为什么按官方线走（不恢复 0801041）

结论：**不恢复服务端下载，保留会话链接 fallback**。理由不是"上游删了所以照做"，而是下面这段代码本身就没法在服务端成立。

### 被删的实现长什么样

`0801041` 的链路：`_batch_execute(LIST_CONVERSATION_TURNS)` 读回会话 → 递归 `_walk_file_entries` 抽稀疏键 `59` 里的附件 → 对每个附件用 `client.client.get(url)` 下载（带 `Origin`/`Referer`）→ 成功则缓存 32 条、发 `/gemini-proxy/taskfile?token=…`，失败回退 Google 直链。

### 关键点：它用的是同一个已登录 session

`client.client` 是 `gemini_webapi` 里那个带 Google cookie 的 `AsyncSession`。也就是说 403 **不是"忘了带 cookie"**，而是带着有效登录态去取 `contribution.usercontent`，Google 仍然拒绝。这类链接的语义是"在生成它的那个浏览器标签里点一下"，服务端拿它当 CDN 直链用是越界的。

`1d7f349` 的替换（`render_task_file_links`，`main.py:369`）保留了唯一确定可用的东西：文件名 + 会话链接，用户在已登录的浏览器里手动导出。它同时保住了文件名，信息没丢。

### 要恢复得先证明什么（现在一条都还没证明）

本地没有 Gemi2Api 的账号 cookie（`.env` 与 `SECURE_1PSID*` 都没配），任何下载实验都无从下手，所以这轮只做到"结论 + 待验证清单"，不动代码。

按性价比排的验证顺序：

1. **同一会话即时下载**：0801041 已经这么做了（生成后立刻在同一 cid 上下载），仍然 403。要重试的话，得确认它下载时用的 client 与生成时是同一个实例、同一个 authuser 槽位。
2. **链接形态**：先打印附件 URL 的 host 与 query（不要打全文，含签名）。如果是 `blob:` 相对路径或带 `authuser=1` 的槽位不匹配，那是纯解析问题，好办。
3. **请求头**：`X-Goog-BatchExecuteRPC` 系列、`X-Same-Domain: 1`，以及 `impersonate="chrome"`（`curl_cffi` 指纹）。缺指纹时 Google 常直接 403 而不是 401。
4. **HTTP 方法**：确认不是 `POST` 拿 redirect。用 `GET` 取 200 但 `text/html`（登录墙）与 403 是两种不同失败，日志里要分开。

只有在第 3 步（或更早）出现 200 且 `content-type` 不是 `text/html` 时，才值得恢复服务端下载；恢复时 `/gemini-proxy/taskfile` 必须带 token 校验、TTL 和大小上限——0801041 的 token 是 `uuid4` 直出，无过期、无上限，32 条上限还会整体 `clear()` 清空别人正在下的文件。

### 本轮实际做了什么

| 项 | 状态 |
|---|---|
| Task 生成（`featureMode 40`、`-task` 后缀、`ensure_task_ready`） | 上游已具备，未改 |
| Task 产物下载 | 维持 fallback，本文档记录待验证清单 |
| 工具契约注入位置 | 已改为并入首条 system 消息（`feat/tool-contract`） |
| 额度/能力话术 | 已防止当终答（非流式 502，流式补说明块） |
| 同请求合并 + 答案重放 + 请求 id | 已实现（`feat/reliability`） |
| pytest 基线 | 29 项，覆盖解析/契约/合并/请求 id |
