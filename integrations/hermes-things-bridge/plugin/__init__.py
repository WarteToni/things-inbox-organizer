"""things-bridge plugin - register add_things_todo / things_bridge_status tools.

The user says e.g. "提醒我周五给老板发周报" to Hermes (via the C*ONE app /
Telegram). The LLM recognizes the reminder intent, extracts {title, when,
notes, list} and calls add_things_todo. The bridge persists the todo to a
durable queue and pushes it to the user's iPhone via Bark; tapping the push
opens Things and creates the todo (synced to iPad / MacBook Pro via Things
Cloud).

Hot-reload: every handler invocation calls bridge.reload_bridge_module() which
re-executes bridge.py from disk. This means edits to bridge.py (e.g. changing
the classification dictionary) take effect on the NEXT tool call without any
gateway restart. If the re-execution fails (syntax error etc.), the previous
module is kept — graceful degradation, never breaks reminder creation.

Design refs: /root/Tech_Lab/projects/hermes-things-bridge/PROJECT.md
All "smart" logic (intent + date parsing) lives in the LLM; the tool schema
instructs it to emit ISO dates. This module is pure plumbing: never raises.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

from . import bridge

logger = logging.getLogger("things_bridge")

TOOLSET = "things_bridge"

_ADD_SCHEMA = {
    "name": "add_things_todo",
    "description": (
        "在用户的 Things 待办 App 里创建一条待办（通过 Bark 推送到 iPhone，"
        "用户点一下通知即创建，自动同步到 iPad/MacBook）。\n"
        "【何时调用】用户明确表达提醒/待办/别忘了/记一下要做某事/帮我记着 时调用；"
        "普通对话、询问、闲聊时绝不调用。\n"
        "【参数要求】title: 简洁的待办标题（动词开头，去掉\"提醒我\"等引导语）；"
        "when: 必须是 YYYY-MM-DD（如 2026-08-14）或关键词 today/tomorrow/evening/anytime/someday。"
        "「周五」要按当前日期换算成具体 ISO 日期；用户没说日期时用 today。\n"
        "【list 参数（可选）】指定待办归入 Things 的哪个 Area/Project。"
        "如果用户明确说了归属（如\"记到Acme\"\"放到Ginkgo\"），传对应的清单名。"
        "不传时系统会根据标题/备注关键词自动归类（Acme/Ginkgo/爸妈/个人），"
        "归不了就进收件箱。可选值参考：Acme IVD / Ginkgo Pharma / Family / Personal。\n"
        "【调用后】用一句话轻确认，如「✅ 已加入 Things：<title>（<日期>）」，不要长篇解释。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "待办标题，简洁明确，如「给老板发周报」",
            },
            "when": {
                "type": "string",
                "description": (
                    "日期：优先 YYYY-MM-DD（基于当前日期换算，如 周五→具体日期）；"
                    "无具体日期时用 today/tomorrow/evening/anytime/someday 之一"
                ),
            },
            "notes": {
                "type": "string",
                "description": "可选备注（用户提到的细节、上下文）",
            },
            "list": {
                "type": "string",
                "description": (
                    "可选，Things 清单/Area/Project 名称。用户明确指定归属时传入"
                    "（如 Acme IVD / Ginkgo Pharma / Family / Personal）；"
                    "不传则系统按关键词自动归类"
                ),
            },
        },
        "required": ["title", "when"],
    },
}

_STATUS_SCHEMA = {
    "name": "things_bridge_status",
    "description": (
        "查看 Hermes→Things 桥接队列状态：有多少待办还没推送成功（Bark 失败时会积压）。"
        "用户问「刚才的提醒发了吗/Things 队列」时调用。可选参数 flush=true 立即重试推送所有积压项。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "flush": {
                "type": "boolean",
                "description": "true=立即重试推送所有积压的待办",
            },
        },
        "required": [],
    },
}


def _handler_add(args: dict[str, Any], **kwargs: Any) -> str:
    # Hot-reload: re-execute bridge.py from disk so code edits take effect
    # without gateway restart. Falls back to current module on error.
    mod = bridge.reload_bridge_module()
    try:
        result = mod.add_things_todo(
            title=args.get("title", ""),
            when=args.get("when", ""),
            notes=args.get("notes", "") or "",
            list_name=args.get("list", "") or "",
        )
    except Exception as e:  # noqa: BLE001 - handlers must never raise
        logger.exception("add_things_todo crashed")
        result = {"ok": False, "error": f"内部错误: {e}"}
    logger.info(
        "add_things_todo title=%r when=%r list=%r ok=%s",
        args.get("title"), args.get("when"), args.get("list"), result.get("ok"),
    )
    return json.dumps(result, ensure_ascii=False)


def _handler_status(args: dict[str, Any], **kwargs: Any) -> str:
    mod = bridge.reload_bridge_module()
    try:
        if args.get("flush"):
            flushed = mod.flush_queue()
            status = mod.queue_status()
            result = {"ok": True, "flush_results": flushed, **status}
        else:
            result = mod.queue_status()
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"内部错误: {e}"}
    return json.dumps(result, ensure_ascii=False)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="add_things_todo",
        toolset=TOOLSET,
        schema=_ADD_SCHEMA,
        handler=_handler_add,
        description="Create a todo in Things via Bark push (tap-to-add)",
        emoji="📥",
    )
    ctx.register_tool(
        name="things_bridge_status",
        toolset=TOOLSET,
        schema=_STATUS_SCHEMA,
        handler=_handler_status,
        description="Things bridge queue status / retry flush",
        emoji="🔁",
    )
    logger.info(
        "things-bridge registered (bark key configured: %s, today=%s)",
        bridge.is_configured(), _dt.date.today().isoformat(),
    )
