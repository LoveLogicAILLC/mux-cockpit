"""
Pi-style JSONL session files: a header line then an append-only tree of entries
linked by id/parentId. Branching = appending from an older leaf (used by rewind).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from .protocol import new_id, now_ms

VERSION = 3


class Session:
    def __init__(self, session_dir: Optional[Path], cwd: str, name: Optional[str] = None,
                 persist: bool = True):
        self.id = new_id()
        self.cwd = cwd
        self.name = name
        self.persist = persist and session_dir is not None
        self.file: Optional[Path] = None
        self.entries: List[dict] = []
        self.leaf: Optional[str] = None
        if self.persist:
            session_dir.mkdir(parents=True, exist_ok=True)
            self.file = session_dir / f"{now_ms()}_{self.id}.jsonl"
            self._write({"type": "session", "version": VERSION, "id": self.id,
                         "timestamp": now_ms(), "cwd": cwd, "name": name})

    def _write(self, obj: dict) -> None:
        if self.file:
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def append(self, entry_type: str, **payload) -> dict:
        e = {"type": entry_type, "id": new_id(), "parentId": self.leaf, "timestamp": now_ms(), **payload}
        self.entries.append(e)
        self.leaf = e["id"]
        self._write(e)
        return e

    def append_message(self, message: dict) -> dict:
        return self.append("message", message=message)

    def branch_to(self, entry_id: Optional[str]) -> None:
        """Move the leaf; the next append forks from here (history stays on disk)."""
        if entry_id is not None and not any(e["id"] == entry_id for e in self.entries):
            raise KeyError(entry_id)
        self.leaf = entry_id
        self.append("branch", target=entry_id)

    def path(self) -> List[dict]:
        """Entries on the active branch, root -> leaf."""
        by_id = {e["id"]: e for e in self.entries}
        out, cur = [], self.leaf
        while cur:
            e = by_id[cur]
            out.append(e)
            cur = e["parentId"]
        return list(reversed(out))

    def messages(self) -> List[dict]:
        """Active-branch messages, honoring the latest compaction entry."""
        msgs: List[dict] = []
        for e in self.path():
            if e["type"] == "compaction":
                msgs = [{"role": "user", "content": "[Conversation summary]\n" + e["summary"],
                         "timestamp": e["timestamp"]}] + list(e.get("kept", []))
            elif e["type"] == "message":
                msgs.append(e["message"])
        return msgs


def load_session(file: Path) -> Session:
    lines = [json.loads(l) for l in file.read_text(encoding="utf-8").splitlines() if l.strip()]
    head = lines[0]
    s = Session.__new__(Session)
    s.id, s.cwd, s.name = head["id"], head.get("cwd", os.getcwd()), head.get("name")
    s.persist, s.file = True, file
    s.entries = lines[1:]
    s.leaf = s.entries[-1]["id"] if s.entries else None
    return s
