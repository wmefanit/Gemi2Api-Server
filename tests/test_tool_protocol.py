"""工具协议回归：围栏解析、流式闸门、指令拼装。

这些用例不访问 Google，只钉住解析层的行为，供后续改动（契约注入位置、
话术拒绝、Task 下载）有回归闸门。
"""

import json

import gemini_tools as gt

TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "run_command",
			"description": "run a shell command",
			"parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
		},
	},
	{
		"type": "function",
		"function": {"name": "read_file", "parameters": {"type": "object"}},
	},
]


def fence(name: str, arguments: dict) -> str:
	return f"{gt.TOOL_FENCE_OPEN}\n{json.dumps({'name': name, 'arguments': arguments})}\n{gt.TOOL_FENCE_CLOSE}"


# ---------------------------------------------------------------------------
# 围栏解析
# ---------------------------------------------------------------------------


def test_parse_single_fence_keeps_prose():
	text = f"我先看一下目录。\n{fence('run_command', {'command': 'ls'})}\n"
	clean, calls = gt.parse_tool_calls(text)
	assert clean.strip() == "我先看一下目录。"
	assert len(calls) == 1
	assert calls[0]["function"]["name"] == "run_command"
	assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}


def test_parse_multiple_fences():
	text = fence("run_command", {"command": "ls"}) + "\n" + fence("read_file", {"path": "a.txt"})
	clean, calls = gt.parse_tool_calls(text)
	assert clean.strip() == ""
	assert [call["function"]["name"] for call in calls] == ["run_command", "read_file"]


def test_parse_inline_fence():
	# 模型有时把整个围栏写在一行里
	text = '```tool_call{"name": "read_file", "arguments": {"path": "b"}}```'
	_clean, calls = gt.parse_tool_calls(text)
	assert len(calls) == 1
	assert json.loads(calls[0]["function"]["arguments"]) == {"path": "b"}


def test_broken_tail_keeps_earlier_calls():
	"""尾块没闭合时，前面已解析的调用必须保留——否则整轮被判死。"""
	text = fence("run_command", {"command": "ls"}) + "\n```tool_call\n{\"name\": \"read_file\", \"argu"
	clean, calls = gt.parse_tool_calls(text)
	assert len(calls) == 1
	assert calls[0]["function"]["name"] == "run_command"
	# 未闭合部分原样留在正文里，方便上层发现异常
	assert "```tool_call" in clean


def test_unparseable_fence_dropped_from_text():
	text = "```tool_call\n{not json}\n```\n后面的话"
	clean, calls = gt.parse_tool_calls(text)
	assert calls == []
	assert "not json" not in clean
	assert "后面的话" in clean


def test_flat_arguments_fallback():
	text = '```tool_call\n{"name": "run_command", "command": "pwd"}\n```'
	_clean, calls = gt.parse_tool_calls(text)
	assert len(calls) == 1
	assert json.loads(calls[0]["function"]["arguments"]) == {"command": "pwd"}


def test_content_without_fence_untouched():
	clean, calls = gt.parse_tool_calls("普通回答，没有任何围栏。")
	assert clean == "普通回答，没有任何围栏。"
	assert calls == []


# ---------------------------------------------------------------------------
# tool_choice 与指令拼装
# ---------------------------------------------------------------------------


def test_parse_tool_choice():
	assert gt.parse_tool_choice("none") == ("none", "")
	assert gt.parse_tool_choice("required") == ("required", "")
	assert gt.parse_tool_choice(None) == ("auto", "")
	assert gt.parse_tool_choice({"type": "function", "function": {"name": "read_file"}}) == ("required", "read_file")


def test_instruction_absent_without_tools():
	assert gt.build_tools_instruction([], None) == ("", "")
	assert gt.build_tools_instruction(None, None) == ("", "")
	assert gt.build_tools_instruction(TOOLS, "none") == ("", "")


def test_instruction_lists_tool_names():
	top, bottom = gt.build_tools_instruction(TOOLS, None)
	assert "run_command" in top and "read_file" in top
	# 末尾锚点必须显式否认「没有工具 / 有调用额度」，这是话术产生的主因
	assert "quota" in bottom
	assert gt.TOOL_FENCE_OPEN in bottom


def test_forced_tool_only_lists_that_tool():
	top, _bottom = gt.build_tools_instruction(TOOLS, {"type": "function", "function": {"name": "read_file"}})
	assert "read_file" in top
	assert "run_command" not in top


# ---------------------------------------------------------------------------
# 流式闸门
# ---------------------------------------------------------------------------


def test_gate_never_leaks_fence_content():
	emitted = []
	gate = gt.ToolFenceGate(emitted.append)
	for chunk in ["先想", "一下```too", "l_call\n{\"name\"", ": \"run_command\"}", "\n```"]:
		gate.push(chunk)
	assert "".join(emitted) == "先想一下"
	clean, calls = gt.parse_tool_calls(gate.sent + "")
	assert calls == []  # 围栏内容从未下发，正文里也不该有调用
	assert clean == "先想一下"


def test_gate_remaining_after_final_text():
	emitted = []
	gate = gt.ToolFenceGate(emitted.append)
	gate.push("第一步")
	final = "第一步，然后结束"
	assert gate.remaining(final) == "，然后结束"
	assert gate.remaining("完全不同") == ""


def test_partial_prefix_len():
	assert gt.partial_prefix_len("abc```tool_c", "```tool_call") == len("```tool_c")
	# 三个反引号本身就是标记的前缀，必须扣住等下一帧，不能先发出去
	assert gt.partial_prefix_len("abc```", "```tool_call") == 3
	assert gt.partial_prefix_len("abc", "```tool_call") == 0
