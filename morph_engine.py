"""
morph_engine.py
Role morphing — AgentsRoom "Morph agent": switch role on the fly, keep the whole session.

A worker IS a harness Agent with a persistent session. Morphing swaps the role prompt
(and the LoRA model tag, when installed) without touching the conversation.
keep_context=False starts a fresh session instead.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from harness.roles import LORA_TAGS, ROLE_PROMPTS, ROLES  # noqa: F401 (re-export)


def lora_for(role: str, provider, base_model: str) -> tuple[str, Optional[str]]:
    """(model_to_call, lora_label). Falls back to base model if the tag isn't installed."""
    tag = LORA_TAGS.get(role)
    if tag and getattr(provider, "name", "") == "ollama":
        try:
            names = set(provider.list_models())
            if tag in names or f"{tag}:latest" in names:
                return tag, tag
        except Exception:
            pass
    return base_model, None


class RoleMorphEngine:
    def __init__(self, provider, base_model: str):
        self.provider = provider
        self.base_model = base_model

    async def morph(self, worker, new_role: str, keep_context: bool = True) -> None:
        new_role = new_role.upper()
        if new_role not in ROLE_PROMPTS:
            raise ValueError(f"unknown role {new_role}; valid: {', '.join(ROLES)}")
        agent = worker.agent
        if agent.is_streaming:
            await agent.abort()
        if not keep_context:
            agent.new_session()
        agent.set_role(new_role, ROLE_PROMPTS[new_role])
        agent.model, worker.lora = lora_for(new_role, self.provider, self.base_model)
        worker.role = new_role


# ---------------------------------------------------------------- output parsing
def extract_json(text: str, key: Optional[str] = None):
    """Return the LAST balanced JSON object in text (roles end with it). With `key`,
    return the last object that contains that key (skips nested sub-objects)."""
    text = re.sub(r"```(?:json)?", "", text or "")
    found = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth, instr, esc = 0, False, False
            for j in range(i, len(text)):
                ch = text[j]
                if instr:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        instr = False
                    continue
                if ch == '"':
                    instr = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            found.append(json.loads(text[i:j + 1]))
                            i = j
                        except json.JSONDecodeError:
                            pass
                        break
        i += 1
    if key:
        found = [f for f in found if isinstance(f, dict) and key in f]
    return found[-1] if found else None


def parse_plan(text: str) -> List[dict]:
    data = extract_json(text, "steps")
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return []
    out = []
    for i, s in enumerate(steps, 1):
        if isinstance(s, dict) and s.get("desc"):
            action = str(s.get("action", "implement")).lower()
            if action not in ("research", "design", "implement", "verify"):
                action = "implement"
            out.append({"id": s.get("id", i), "action": action, "desc": str(s["desc"])[:500]})
    return out[:8]


def parse_score(text: str) -> Optional[float]:
    """None when unparseable — an unscored output is NOT a pass (original defaulted to 6.5)."""
    data = extract_json(text, "score")
    if isinstance(data, dict) and "score" in data:
        try:
            return max(0.0, min(10.0, float(data["score"])))
        except (TypeError, ValueError):
            return None
    m = re.search(r'"?score"?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)', text or "")
    return max(0.0, min(10.0, float(m.group(1)))) if m else None


def parse_fix(text: str) -> Optional[str]:
    """Critic's suggested correction on rejection. None when absent/unparseable."""
    data = extract_json(text, "fix")
    if isinstance(data, dict) and data.get("fix"):
        return str(data["fix"])[:400]
    return None
