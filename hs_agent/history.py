"""归类记录库：每次归类的结果存进本地 SQLite（agent_data/history.db），用来复用、人工确认和修正。

- 描述完全相同（忽略空格和标点）的商品，直接沿用以前的结果，不再调用大模型；
- 人工确认或修正过的记录是「判例」：归类相似商品时，最相似的几条会先给 Agent 参考；
- 修正时只填正确的编码和一句理由，退税率等按当前数据表重新查。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .cn_tariff import CnTariff
from .tariff import Tariff, fmt_code, normalize_code
from .toolbox import Classification, Step

STATUS_NAMES = {"agent": "Agent 结果", "confirmed": "人工确认", "corrected": "人工修正"}
PRECEDENT_MIN = 0.35  # 相似度低于这个就不当判例
PRECEDENT_K = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    description TEXT NOT NULL,
    norm TEXT NOT NULL,
    answer TEXT NOT NULL,          -- Agent 给出的完整结果（JSON）
    code TEXT NOT NULL,            -- 当前采用的编码：修正过就是修正后的
    status TEXT NOT NULL DEFAULT 'agent',
    note TEXT NOT NULL DEFAULT '',
    reviewed_at REAL,
    run_id TEXT NOT NULL DEFAULT '',
    reuse_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS records_norm ON records(norm);
"""

_PUNCT = re.compile(r"[\s　，。、；：！？（）()\[\]【】“”\"'‘’,.;:!?/\\|%＋+\-—_*·~`<>《》]+")


def normalize(text: str) -> str:
    return _PUNCT.sub("", text).lower()


def grams(text: str) -> set[str]:
    """中文按相邻两个字切分，英文和数字按整词。"""
    out: set[str] = set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower()))
    for run in re.findall(r"[一-鿿]+", text):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            out.add(run)
    return out


def similarity(a: set[str], b: set[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


@dataclass
class Record:
    id: int
    created: float
    description: str
    answer: Classification
    code: str
    status: str
    note: str
    reviewed_at: float | None
    run_id: str
    reuse_count: int

    @property
    def status_name(self) -> str:
        return STATUS_NAMES.get(self.status, self.status)

    def public(self) -> dict:
        return {
            "id": self.id, "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created)),
            "description": self.description, "code": fmt_code(self.code), "agent_code": fmt_code(self.answer.cn_code or self.answer.hs6),
            "status": self.status, "status_name": self.status_name, "note": self.note,
            "confidence": self.answer.confidence, "need_review": self.answer.need_review and self.status == "agent",
            "reuse_count": self.reuse_count,
        }


class HistoryError(Exception):
    pass


class History:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _rec(self, row: sqlite3.Row) -> Record:
        return Record(row["id"], row["created"], row["description"], Classification.model_validate_json(row["answer"]),
                      row["code"], row["status"], row["note"], row["reviewed_at"], row["run_id"], row["reuse_count"])

    def _query(self, sql: str, args: tuple = ()) -> list[Record]:
        with self._lock:
            return [self._rec(r) for r in self._db.execute(sql, args).fetchall()]

    # ---------- 写 ----------

    def add(self, description: str, answer: Classification, run_id: str = "") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO records(created, description, norm, answer, code, run_id) VALUES (?,?,?,?,?,?)",
                (time.time(), description, normalize(description), answer.model_dump_json(),
                 answer.cn_code or answer.hs6, run_id))
            return cur.lastrowid

    def confirm(self, rid: int) -> Record:
        rec = self.get(rid)
        with self._lock, self._db:
            self._db.execute("UPDATE records SET status=?, reviewed_at=? WHERE id=?",
                             ("corrected" if rec.status == "corrected" else "confirmed", time.time(), rid))
        return self.get(rid)

    def correct(self, rid: int, code: str, note: str, tariff: Tariff, cn: CnTariff) -> Record:
        self.get(rid)
        code = check_code(code, tariff, cn)
        with self._lock, self._db:
            self._db.execute("UPDATE records SET code=?, status='corrected', note=?, reviewed_at=? WHERE id=?",
                             (code, note.strip(), time.time(), rid))
        return self.get(rid)

    def touch(self, rid: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE records SET reuse_count = reuse_count + 1 WHERE id=?", (rid,))

    # ---------- 读 ----------

    def get(self, rid: int) -> Record:
        recs = self._query("SELECT * FROM records WHERE id=?", (rid,))
        if not recs:
            raise HistoryError(f"没有编号为 {rid} 的记录")
        return recs[0]

    def find_exact(self, description: str) -> Record | None:
        """描述相同的记录：人工修正 > 人工确认 > 最近一次 Agent 结果。"""
        recs = self._query(
            "SELECT * FROM records WHERE norm=? ORDER BY CASE status WHEN 'corrected' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END,"
            " COALESCE(reviewed_at, created) DESC LIMIT 1", (normalize(description),))
        return recs[0] if recs else None

    def similar(self, description: str, k: int = PRECEDENT_K, min_score: float = PRECEDENT_MIN,
                reviewed_only: bool = True) -> list[tuple[Record, float]]:
        where = "WHERE status IN ('confirmed','corrected')" if reviewed_only else ""
        target, norm = grams(description), normalize(description)
        scored, seen = [], set()
        for rec in self._query(f"SELECT * FROM records {where} ORDER BY COALESCE(reviewed_at, created) DESC"):
            key = normalize(rec.description)
            if key == norm or key in seen:  # 完全相同的走复用；同一描述只取最新的一条
                continue
            seen.add(key)
            score = similarity(target, grams(rec.description))
            if score >= min_score:
                scored.append((rec, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def list(self, q: str = "", status: str = "", limit: int = 100) -> list[Record]:
        sql, args = "SELECT * FROM records WHERE 1=1", []
        if q:
            sql += " AND (description LIKE ? OR code LIKE ?)"
            args += [f"%{q}%", f"%{re.sub(r'[^0-9]', '', q) or q}%"]
        if status == "review":
            sql += " AND status='agent' AND json_extract(answer, '$.need_review')"
        elif status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self._query(sql, tuple(args))

    def counts(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) n FROM records GROUP BY status").fetchall()
        c = {r["status"]: r["n"] for r in rows}
        return {"total": sum(c.values()), "confirmed": c.get("confirmed", 0), "corrected": c.get("corrected", 0)}


_open: dict[Path, History] = {}
_open_lock = threading.Lock()


def open_history(path: Path) -> History:
    """同一个文件在进程里只打开一次（网页和批量会并发使用）。"""
    path = path.resolve()
    with _open_lock:
        if path not in _open:
            _open[path] = History(path)
        return _open[path]


def check_code(code: str, tariff: Tariff, cn: CnTariff) -> str:
    """人工填写的编码：6 位必须是税则里的子目；导入了中国税则时，更长的编码必须在那个子目下面。"""
    code = normalize_code(code)
    if len(code) < 6 or not code.isdigit():
        raise HistoryError("请填写至少 6 位的编码，例如 8516.60 或 8516.60.9000。")
    if not tariff.is_leaf(code[:6]):
        raise HistoryError(f"{fmt_code(code[:6])} 不是税则里存在的 6 位子目。")
    if len(code) > 6 and cn.loaded and cn.under(code[:6]):
        valid = {it.code for it in cn.under(code[:6])}
        if code not in valid:
            raise HistoryError(f"中国税则里没有 {fmt_code(code)}，{fmt_code(code[:6])} 下面的编码有：" + "、".join(fmt_code(c) for c in sorted(valid)))
    return code


def answer_of(rec: Record, tariff: Tariff, cn: CnTariff, review_threshold: float) -> Classification:
    """记录对应的结果：修正过的换成修正后的编码；退税率、监管条件按当前数据表重新查。"""
    a = rec.answer.model_copy(deep=True)
    code = rec.code
    if rec.status == "corrected" and code != (a.cn_code or a.hs6):
        node = tariff.get(code[:6])
        old = a.cn_code or a.hs6
        a.hs6, a.hs6_desc, a.path = node.code, node.desc, tariff.path_text(node.code)
        a.steps = [Step(code=fmt_code(code), reason=f"人工修正（原为 {fmt_code(old)}）：{rec.note or '未写理由'}")] + a.steps
        a.rules = ["人工修正"] + a.rules
        a.alternatives = [x for x in [old[:6]] + a.alternatives if x != node.code]
        a.cn_code = code if len(code) > 6 else ""
    if rec.status in ("confirmed", "corrected"):
        a.confidence, a.need_review = 1.0, False
    else:
        a.need_review = a.confidence < review_threshold
    item = cn.get(a.cn_code) if a.cn_code and cn.loaded else None
    a.cn_name = item.name if item else ""
    a.refund_rate = item.refund_rate if item else None
    a.conditions = item.conditions if item else ""
    a.vat_rate = item.vat_rate if item else ""
    a.refund_note = item.note if item else ""
    a.data_version = item.version if item else ""
    return a


def precedents_text(items: list[tuple[Record, float]]) -> str:
    if not items:
        return ""
    lines = ["", "参考判例：以下是以前人工确认或修正过的相似商品。关键要素（材质、功能、用途、结构）和本商品一致时，应沿用判例的编码，"
             "并在 steps 里引用；有差异时按本商品的实际情况判断，并写明和判例哪里不同。判例的编码同样要用 browse 核对后才能提交。"]
    for i, (rec, score) in enumerate(items, 1):
        how = f"人工修正，理由：{rec.note}" if rec.status == "corrected" and rec.note else rec.status_name
        lines.append(f"{i}. 商品：{rec.description[:300]}\n   编码：{fmt_code(rec.code)}（{how}；相似度 {score:.0%}）")
    return "\n".join(lines)
