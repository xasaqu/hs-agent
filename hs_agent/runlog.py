"""运行日志：每一步的工具调用和结果都写进 JSONL，方便回看 Agent 的归类路径。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable


class RunLog:
    def __init__(self, runs_dir: Path, task: str, listener: Callable[[dict], None] | None = None):
        self.listener = listener  # 网页界面用它实时显示进度
        self.run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.path = runs_dir / f"{self.run_id}.jsonl"
        self.started = time.time()
        self.steps = 0
        self.questions = 0
        self.write("start", {"task": task})

    def write(self, event: str, data: dict) -> None:
        record = {"t": round(time.time() - self.started, 2), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.listener:
            self.listener(record)

    def action(self, tool: str, args: dict, ok: bool = True, note: str = "") -> None:
        self.steps += 1
        self.write("action", {"step": self.steps, "tool": tool, "args": args, "ok": ok, "note": note})

    @property
    def elapsed(self) -> float:
        return round(time.time() - self.started, 1)
