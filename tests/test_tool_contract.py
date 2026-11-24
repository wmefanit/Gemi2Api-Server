"""工具契约的注入位置与文案约束。

真号实测（Gemini Web，同一组工具、同一句契约）：
拼在用户回合里 -> 模型称"工具只是提示词里的文本示例"，拒绝调用；
作为独立 system 消息 -> 仍然拒绝；
并入首条 system 消息 -> 立刻发出工具调用。
Gemini 网页端只保留一条 system instruction，契约落在它之外就只是普通文本。

拒答只在提示词层治理：没有关键词匹配、没有补问重试。所以这里的用例盯的是
「契约文案必须包含哪几条硬约束」，删掉任何一条都会被这里挡住。
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
# 契约文案的硬约束（拒答治理全部落在这几条上）
# ---------------------------------------------------------------------------


def test_bottom_anchor_forbids_refusing_and_invented_quota():
	_top, bottom = gt.build_tools_instruction(TOOLS)
	# 工具真实存在、由服务端执行
	assert "executed by the server" in bottom
	# 不存在调用额度/计数器
	assert "no quota, no counter" in bottom
	# 需要工具时不许解释式拒答
	assert "Never refuse on your own" in bottom
	# 不需要工具时才允许散文回答，且不许提工具
	assert "needs no tool at all" in bottom


def test_bottom_anchor_demands_single_fence_and_warns_off_plain_code_block():
	_top, bottom = gt.build_tools_instruction(TOOLS)
	assert "exactly ONE" in bottom
	assert "NOT executed" in bottom


def test_forced_tool_contract_keeps_its_name():
	top, _bottom = gt.build_tools_instruction(TOOLS, {"type": "function", "function": {"name": "run_command"}})
	assert 'MUST call the tool "run_command"' in top


def test_tool_choice_none_drops_contract():
	assert gt.build_tools_instruction(TOOLS, "none") == ("", "")
