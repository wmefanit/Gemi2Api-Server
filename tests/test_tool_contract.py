"""工具契约的注入位置与话术检测。

真号实测（Gemini Web，同一组工具、同一句契约）：
拼在用户回合里 -> 模型称"工具只是提示词里的文本示例"，拒绝调用；
作为独立 system 消息 -> 仍然拒绝；
并入首条 system 消息 -> 立刻发出工具调用。
Gemini 网页端只保留一条 system instruction，契约落在它之外就只是普通文本。
"""

import gemini_tools as gt
import main

TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "run_command",
			"description": "run a shell command",
			"parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
		},
	},
]


def conversation(messages, **kwargs):
	text, _files = main.prepare_conversation(messages, **kwargs)
	return text


def test_contract_merges_into_first_system_message():
	text = conversation(
		[main.Message(role="system", content="你是运维助手"), main.Message(role="user", content="列出目录")],
		tools=TOOLS,
	)
	assert text.startswith("System: " + gt.CONTRACT_OPEN)
	# 客户端自己的提示仍在同一条 system 消息里，契约在它之前
	assert text.index(gt.CONTRACT_OPEN) < text.index("你是运维助手")
	# 只有一条 system 块：契约没有被拆成第二条 system 消息
	assert text.count("\nSystem: ") + text.count("System: ") == 1


def test_contract_creates_leading_system_when_client_sends_none():
	text = conversation([main.Message(role="user", content="列出目录")], tools=TOOLS)
	assert text.startswith("System: " + gt.CONTRACT_OPEN)
	assert "Human: 列出目录" in text


def test_no_contract_without_tools():
	text = conversation([main.Message(role="system", content="你是运维助手"), main.Message(role="user", content="你好")])
	assert gt.CONTRACT_OPEN not in text
	assert text.startswith("System: 你是运维助手")


def test_gem_mode_keeps_contract_out_of_system_message():
	"""启用 Gem 时不新建 system 消息，否则和 Gem 自带 prompt 冲突。"""
	text = conversation([main.Message(role="user", content="列出目录")], skip_system=True, tools=TOOLS)
	assert gt.CONTRACT_OPEN not in text
	assert text.startswith("[System instruction]")
	assert "Human: 列出目录" in text


def test_bottom_anchor_stays_last():
	text = conversation([main.Message(role="user", content="列出目录")], tools=TOOLS)
	bottom_at = text.index("highest priority")
	assert bottom_at > text.index("列出目录")
	assert text.rstrip().endswith("Assistant:")


# ---------------------------------------------------------------------------
# 话术检测
# ---------------------------------------------------------------------------


def test_quota_claim_detected():
	assert gt.looks_like_tool_quota_claim("工具调用额度已锁死（剩余 0 次），请提供更多额度")
	assert gt.looks_like_tool_quota_claim("You have 0 tool calls left.")
	assert not gt.looks_like_tool_quota_claim("已执行 ls -la，输出如下：")


def test_unavailable_claim_detected():
	assert gt.looks_like_tool_unavailable_claim("我无法访问文件系统，请把内容贴给我")
	assert gt.looks_like_tool_unavailable_claim("I don't have access to the terminal.")
	assert not gt.looks_like_tool_unavailable_claim("文件已读取，内容是 hello")
