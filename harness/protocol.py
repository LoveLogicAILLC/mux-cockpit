"""Message / content / usage shapes matching Pi's message-types.md."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def usage(inp: int = 0, out: int = 0) -> dict:
    return {"input": inp, "output": out, "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": inp + out,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


def user_message(content: str) -> dict:
    return {"role": "user", "content": content, "timestamp": now_ms()}


def assistant_message(provider: str, model: str) -> dict:
    return {"role": "assistant", "content": [], "api": provider, "provider": provider,
            "model": model, "usage": usage(), "stopReason": "pending", "timestamp": now_ms()}


def tool_result_message(call_id: str, name: str, content: List[dict], is_error: bool,
                        details: Optional[dict] = None) -> dict:
    m = {"role": "toolResult", "toolCallId": call_id, "toolName": name, "content": content,
         "isError": is_error, "timestamp": now_ms()}
    if details:
        m["details"] = details
    return m


def message_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c or [] if b.get("type") == "text")


def tool_calls(msg: dict) -> List[dict]:
    return [b for b in msg.get("content", []) if isinstance(b, dict) and b.get("type") == "toolCall"]


def estimate_tokens(messages: List[dict]) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        else:
            for b in c or []:
                n += len(b.get("text", "") or b.get("thinking", "") or str(b.get("arguments", "")))
    return n // 4


def response(cmd: str, req_id: Optional[str], success: bool = True, data: Any = None,
             error: Optional[str] = None) -> Dict[str, Any]:
    r: Dict[str, Any] = {"type": "response", "command": cmd, "success": success}
    if req_id is not None:
        r["id"] = req_id
    if data is not None:
        r["data"] = data
    if error is not None:
        r["error"] = error
    return r
