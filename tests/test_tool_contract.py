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
			"parameters": {
				"type": "object",
				"properties": {"command": {"type": "string"}},
				"required": ["command"],
			},
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
	assert text.startswith("System: <caller_tool_interface>")
	# 客户端自己的提示仍在同一条 system 消息里，契约在它之前
	assert text.index("Caller tool definitions") < text.index("你是运维助手")
	# 契约不再用哨兵包裹：模型会把标记当成人贴的假文档
	assert "GATEWAY_TOOL_CONTRACT" not in text
	# 只有一条 system 块：契约没有被拆成第二条 system 消息
	assert text.count("\nSystem: ") + text.count("System: ") == 1


def test_contract_creates_leading_system_when_client_sends_none():
	text = conversation([main.Message(role="user", content="列出目录")], tools=TOOLS)
	assert text.startswith("System: <caller_tool_interface>")
	assert "Human: 列出目录" in text


def test_no_contract_without_tools():
	text = conversation([main.Message(role="system", content="你是运维助手"), main.Message(role="user", content="你好")])
	assert "Caller tool definitions" not in text
	assert text.startswith("System: 你是运维助手")


def test_gem_mode_keeps_contract_out_of_system_message():
	"""启用 Gem 时不新建 system 消息，否则和 Gem 自带 prompt 冲突。"""
	text = conversation([main.Message(role="user", content="列出目录")], skip_system=True, tools=TOOLS)
	assert text.startswith("<caller_tool_interface>")
	assert "Human: 列出目录" in text


def test_bottom_anchor_stays_last():
	text = conversation([main.Message(role="user", content="列出目录")], tools=TOOLS)
	bottom_at = text.index("<caller_tool_interface_reminder>")
	assert bottom_at > text.index("列出目录")
	assert text.rstrip().endswith("Assistant:")


# ---------------------------------------------------------------------------
# 契约文案的硬约束（拒答治理全部落在这几条上）
# ---------------------------------------------------------------------------


def test_bottom_anchor_forbids_refusing_and_invented_quota():
	_top, bottom = gt.build_tools_instruction(TOOLS)
	# 工具真实存在、由服务端执行
	assert "part of the API caller's instructions" in _top
	assert "executed by the API caller" in _top
	assert "no call quota, counter or budget" in bottom
	# 不存在调用额度/计数器
	# 需要工具时不许解释式拒答
	assert "no claim that you lack" in bottom
	# 不需要工具时才允许散文回答，且不许提工具
	assert "If no tool is needed" in bottom


def test_bottom_anchor_demands_single_fence_and_warns_off_plain_code_block():
	_top, bottom = gt.build_tools_instruction(TOOLS)
	assert "exactly one marker" in bottom
	assert "bash/python block" in bottom
	assert "/etc/hostname" not in bottom
	assert '"command": "ls -la"' in bottom

	# Examples must contain valid required arguments and remain workspace-local.
	read_only = [{
		"type": "function",
		"function": {
			"name": "read_file",
			"parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
		},
	}]
	_, read_bottom = gt.build_tools_instruction(read_only)
	assert '"path": "README.md"' in read_bottom
	assert '"arguments": {}' not in read_bottom

	write_only = [{
		"type": "function",
		"function": {
			"name": "write_file",
			"parameters": {
				"type": "object",
				"properties": {"path": {"type": "string"}, "content": {"type": "string"}},
				"required": ["path", "content"],
			},
		},
	}]
	_, write_bottom = gt.build_tools_instruction(write_only)
	assert '"name": "write_file"' not in write_bottom
	assert "Do not issue a sample call" in write_bottom
	assert "only with arguments grounded in the user's request" in write_bottom

	# An optional-only schema still gets a small workspace-local example.
	optional_read = [{
		"type": "function",
		"function": {
			"name": "read_file",
			"parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
		},
	}]
	_, optional_bottom = gt.build_tools_instruction(optional_read)
	assert '"path": "README.md"' in optional_bottom

	# Enum, const and nested required properties follow their declared schema.
	complex_tool = [{
		"type": "function",
		"function": {
			"name": "inspect",
			"parameters": {
				"type": "object",
				"properties": {
					"options": {"type": "object", "required": ["mode", "format"], "properties": {
						"mode": {"type": "string", "enum": ["safe", "full"]},
						"format": {"type": "string", "const": "json"},
					}},
				},
				"required": ["options"],
			},
		},
	}]
	_, complex_bottom = gt.build_tools_instruction(complex_tool)
	assert '"options": {"mode": "safe", "format": "json"}' in complex_bottom

	# Mutating tools, including camelCase and unfamiliar APIs, must not get runnable samples.
	mutating = [{
		"type": "function",
		"function": {
			"name": "sendMessage",
			"parameters": {
				"type": "object",
				"properties": {"recipient": {"type": "string"}, "body": {"type": "string"}},
				"required": ["recipient", "body"],
			},
		},
	}]
	_, mutating_bottom = gt.build_tools_instruction(mutating)
	assert '"name": "sendMessage"' not in mutating_bottom
	assert "Do not issue a sample call" in mutating_bottom

	unknown = [{
		"type": "function",
		"function": {
			"name": "accountAction",
			"parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}},
		},
	}]
	_, unknown_bottom = gt.build_tools_instruction(unknown)
	assert '"name": "accountAction"' not in unknown_bottom
	assert "Do not issue a sample call" in unknown_bottom


def test_tool_contract_requires_verified_code_change_loop():
	top, bottom = gt.build_tools_instruction(TOOLS)
	assert "regression tests" in top
	assert "full suite passes after the final edit" in bottom
	assert "run baseline tests" in top
	assert "full relevant suite after the LAST edit" in top
	assert "fix and rerun failures" in top
	for check in ("git diff --check", "git diff", "git status --short"):
		assert check in top
		assert check in bottom
	assert "After every result call the next" in top
	assert "do not answer before all checks pass" in top
	assert "no proposal or unverified final answer" in bottom
	assert "never invent access or quota limits" in top
	assert "If a check fails, fix the reported issue" in bottom
	assert "rerun the full suite and all three checks" in bottom


def test_forced_tool_contract_keeps_its_name():
	top, _bottom = gt.build_tools_instruction(TOOLS, {"type": "function", "function": {"name": "run_command"}})
	assert 'You must call the tool "run_command"' in top


def test_tool_choice_none_drops_contract():
	assert gt.build_tools_instruction(TOOLS, "none") == ("", "")
