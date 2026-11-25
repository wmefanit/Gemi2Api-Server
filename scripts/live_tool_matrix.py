#!/usr/bin/env python3
"""对 /v1/chat/completions 跑一组 agentic（vibe coding）场景，统计工具调用命中率。

只做观测，不修改服务。用法：
    python scripts/live_tool_matrix.py [--base http://127.0.0.1:18081] [--gap 8]

设计要点：
- 每个场景的 user 文案都不同，避免触发 15 分钟答案重放（request_cache）。
- 场景区分 expect=tool（该发工具调用）与 expect=text（该直接回答）。
- 失败只记录，不重试；命中率按场景类型分组统计。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

READ_FILE = {
	"type": "function",
	"function": {
		"name": "read_file",
		"description": "读取本地文件内容",
		"parameters": {
			"type": "object",
			"properties": {"path": {"type": "string", "description": "绝对或相对路径"}},
			"required": ["path"],
		},
	},
}

RUN_COMMAND = {
	"type": "function",
	"function": {
		"name": "run_command",
		"description": "在用户机器上执行 shell 命令并返回 stdout/stderr",
		"parameters": {
			"type": "object",
			"properties": {
				"command": {"type": "string"},
				"cwd": {"type": "string"},
				"timeout": {"type": "integer", "minimum": 1, "maximum": 600},
			},
			"required": ["command"],
		},
	},
}

WRITE_FILE = {
	"type": "function",
	"function": {
		"name": "write_file",
		"description": "写入文件（覆盖）",
		"parameters": {
			"type": "object",
			"properties": {"path": {"type": "string"}, "content": {"type": "string"}},
			"required": ["path", "content"],
		},
	},
}

# 复杂 schema：嵌套对象 / 枚举 / 数组，验证模型仍能产出合法 arguments
DEPLOY = {
	"type": "function",
	"function": {
		"name": "deploy",
		"description": "部署一个服务到指定环境",
		"parameters": {
			"type": "object",
			"properties": {
				"service": {"type": "string"},
				"env": {"type": "string", "enum": ["dev", "staging", "prod"]},
				"replicas": {"type": "integer", "minimum": 1, "maximum": 10},
				"tags": {"type": "array", "items": {"type": "string"}},
				"resources": {
					"type": "object",
					"properties": {
						"cpu": {"type": "string"},
						"memory": {"type": "string"},
					},
					"required": ["cpu", "memory"],
				},
				"strategy": {"type": "string", "enum": ["rolling", "blue-green", "canary"]},
			},
			"required": ["service", "env", "resources"],
		},
	},
}

# 模拟 Claude Code / Cline 这类客户端塞进来的长开发者提示
LONG_DEVELOPER_PROMPT = (
	"You are a coding agent operating on the user's machine.\n"
	+ "".join(f"Rule {i}: keep changes minimal, run tests after editing, never invent APIs, "
		f"prefer explicit error handling, document non-obvious decisions.\n" for i in range(1, 90))
	+ "Always prefer to act rather than describe. When a shell command or file read is needed, "
	"use the provided tools instead of pasting commands for the user.\n"
)


def post(base: str, payload: dict, timeout: float = 180.0) -> dict:
	body = json.dumps(payload).encode()
	req = urllib.request.Request(
		base.rstrip("/") + "/v1/chat/completions",
		data=body,
		headers={"Content-Type": "application/json"},
	)
	try:
		with urllib.request.urlopen(req, timeout=timeout) as resp:
			return {"status": resp.status, "json": json.loads(resp.read().decode())}
	except urllib.error.HTTPError as exc:
		raw = exc.read().decode(errors="replace")
		try:
			data = json.loads(raw)
		except Exception:
			data = {"raw": raw[:300]}
		return {"status": exc.code, "json": data}
	except Exception as exc:  # 连接层失败（超时 / 断连）
		return {"status": 0, "json": {"error": repr(exc)[:200]}}


def classify(result: dict) -> tuple[str, str]:
	"""返回 (tool|text|error, 一行摘要)。"""
	status = result["status"]
	data = result["json"]
	if status != 200:
		return "error", f"HTTP {status} {str(data)[:90]}"
	choices = data.get("choices") or []
	if not choices:
		return "error", "no choices"
	msg = choices[0].get("message") or {}
	calls = msg.get("tool_calls") or []
	content = (msg.get("content") or "").replace("\n", " ")
	if calls:
		fn = calls[0].get("function") or {}
		return "tool", f"{fn.get('name')}({(fn.get('arguments') or '')[:80]})"
	return "text", content[:110]


SCENARIOS = []


def scenario(name, expect, tools, messages, **extra):
	SCENARIOS.append(
		{"name": name, "expect": expect, "tools": tools, "messages": messages, "extra": extra}
	)


# 1. 典型 vibe：先看目录
scenario(
	"第一步：看项目结构",
	"tool",
	[RUN_COMMAND, READ_FILE],
	[{"role": "system", "content": LONG_DEVELOPER_PROMPT}, {"role": "user", "content": "帮我看看这个项目结构，然后告诉我入口文件在哪"}],
)

# 2. 长开发者提示 + 具体改代码任务
scenario(
	"长 developer prompt + 改 bug",
	"tool",
	[RUN_COMMAND, READ_FILE, WRITE_FILE],
	[
		{"role": "system", "content": LONG_DEVELOPER_PROMPT},
		{"role": "user", "content": "app/main.py 第 42 行有个 KeyError，定位并修掉它"},
	],
)

# 3. 复杂 schema：枚举 + 嵌套 + 数组
scenario(
	"复杂 schema 部署工具",
	"tool",
	[DEPLOY],
	[{"role": "user", "content": "把 order-service 部署到 staging，2 个副本，cpu 500m、内存 1Gi，滚动更新"}],
)

# 4. 只需要工具结果才能答（信息不在上下文）
scenario(
	"信息不在上下文必须读",
	"tool",
	[READ_FILE, RUN_COMMAND],
	[{"role": "user", "content": "config.yaml 里 listen 端口配的是多少？"}],
)

# 5. 明确纯问答：不该调用
scenario(
	"纯问答不该调用",
	"text",
	[RUN_COMMAND, READ_FILE],
	[{"role": "user", "content": "解释一下 TCP 和 UDP 的区别，两句话"}],
)

# 6. 对话里已经有工具往返历史
scenario(
	"多轮历史后继续",
	"tool",
	[RUN_COMMAND, READ_FILE],
	[
		{"role": "user", "content": "先看看 tests 目录"},
		{"role": "assistant", "content": None, "tool_calls": [
			{"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "ls tests"})}}
		]},
		{"role": "tool", "tool_call_id": "c1", "content": "test_api.py\ntest_utils.py"},
		{"role": "assistant", "content": "tests 目录下有两个文件：test_api.py、test_utils.py。"},
		{"role": "user", "content": "那 test_api.py 里测了哪些接口？"},
	],
)

# 7. 上一步失败（exit 1），模型该换路子继续而不是放弃
scenario(
	"上一步失败后继续",
	"tool",
	[RUN_COMMAND, READ_FILE],
	[
		{"role": "user", "content": "跑一下项目测试"},
		{"role": "assistant", "content": None, "tool_calls": [
			{"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "pytest -q"})}}
		]},
		{"role": "tool", "tool_call_id": "c1", "content": "Exit code: 2\nOutput:\nERROR: file or directory not found: pytest"},
		{"role": "user", "content": "那用项目自己的方式跑"},
	],
)

# 8. 用户明确要求只解释、不要动文件
scenario(
	"用户禁止动手",
	"text",
	[RUN_COMMAND, WRITE_FILE],
	[{"role": "user", "content": "别改任何文件，只用文字告诉我怎么手动备份这个目录"}],
)

# 9. 大工具结果之后的收尾轮
scenario(
	"大结果后总结",
	"text",
	[RUN_COMMAND],
	[
		{"role": "user", "content": "统计一下日志里有多少错误"},
		{"role": "assistant", "content": None, "tool_calls": [
			{"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "grep -c ERROR app.log"})}}
		]},
		{"role": "tool", "tool_call_id": "c1", "content": "37"},
		{"role": "user", "content": "那就是 37 条？给我一句话结论"},
	],
)

# 10. 写操作：用户要求改文件
scenario(
	"写文件任务",
	"tool",
	[READ_FILE, WRITE_FILE],
	[{"role": "user", "content": "在 README.md 末尾加一节「本地开发」，写上三步启动命令"}],
)

# 11. 中文口语化的排查任务
scenario(
	"口语排查",
	"tool",
	[RUN_COMMAND, READ_FILE],
	[{"role": "user", "content": "服务起不来了，帮我看看是不是端口被占了"}],
)

# 12. 流式 + 工具（客户端开 stream）
scenario(
	"流式开工具",
	"tool",
	[RUN_COMMAND],
	[{"role": "user", "content": "stream 模式下也帮我列一下当前目录"}],
	stream=True,
)


def run(base: str, gap: float) -> int:
	print(f"base={base} 场景={len(SCENARIOS)} gap={gap}s", flush=True)
	rows = []
	for idx, sc in enumerate(SCENARIOS, 1):
		payload = {
			"model": "gemini-4-hydrogen",
			"max_tokens": 512,
			"messages": sc["messages"],
		}
		if sc["tools"]:
			payload["tools"] = sc["tools"]
		stream = bool(sc["extra"].get("stream"))
		if stream:
			payload["stream"] = True
			res = post_stream(base, payload)
		else:
			res = post(base, payload)
		kind, detail = classify(res)
		ok = (kind == sc["expect"]) or (sc["expect"] == "tool" and kind == "tool")
		rows.append((idx, sc["name"], sc["expect"], kind, ok, detail))
		print(
			f"  {idx:2d}. {'PASS' if ok else 'FAIL'} [{sc['expect']}->{kind}] {sc['name']} | {detail}",
			flush=True,
		)
		if idx != len(SCENARIOS):
			time.sleep(gap)

	total = len(rows)
	passed = sum(1 for r in rows if r[4])
	print("\n=== 汇总", flush=True)
	for label, want in (("应调用工具", "tool"), ("应直接回答", "text")):
		group = [r for r in rows if r[2] == want]
		hit = sum(1 for r in group if r[4])
		print(f"  {label}: {hit}/{len(group)}", flush=True)
	errs = [r for r in rows if r[3] == "error"]
	print(f"  总：{passed}/{total}；上游错误 {len(errs)} 条", flush=True)
	for r in errs:
		print(f"    - #{r[0]} {r[1]}: {r[5]}", flush=True)
	return 0 if passed == total else 1


def post_stream(base: str, payload: dict, timeout: float = 180.0) -> dict:
	"""流式：只关心是否出现 tool_calls 增量和是否报错。"""
	body = json.dumps(payload).encode()
	req = urllib.request.Request(
		base.rstrip("/") + "/v1/chat/completions",
		data=body,
		headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
	)
	try:
		with urllib.request.urlopen(req, timeout=timeout) as resp:
			raw = resp.read().decode(errors="replace")
	except urllib.error.HTTPError as exc:
		return {"status": exc.code, "json": {"raw": exc.read().decode(errors="replace")[:300]}}
	except Exception as exc:
		return {"status": 0, "json": {"error": repr(exc)[:200]}}

	calls = []
	for line in raw.splitlines():
		if not line.startswith("data: ") or line == "data: [DONE]":
			continue
		try:
			chunk = json.loads(line[6:])
		except Exception:
			continue
		for choice in chunk.get("choices") or []:
			for call in (choice.get("delta") or {}).get("tool_calls") or []:
				calls.append(call)
	if calls:
		name = (calls[0].get("function") or {}).get("name", "?")
		return {"status": 200, "json": {"choices": [{"message": {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(calls[0].get("function", {}).get("arguments"))}}]}}]}}
	return {"status": 200, "json": {"choices": [{"message": {"content": raw[:200]}}]}}


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--base", default="http://127.0.0.1:18081")
	parser.add_argument("--gap", type=float, default=8.0)
	args = parser.parse_args()
	sys.exit(run(args.base, args.gap))
