"""Agent 可调用的全部工具。纯 Python 方法，方便脱离大模型单独测试；agent.py 再把它们包装成工具。

关键约束都在代码里，不靠提示词：
- 最终编码必须是税则树里真实存在的 6 位子目；
- 提交前必须浏览过它所在的品目（看过所有同级子目），保证是「逐层比较」选出来的；
- 导入了中国税则时，10 位编码必须是该子目下真实存在的编码；
- 退税率、监管条件由程序查表填写，模型不能自己写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic import BaseModel

from .cn_tariff import CnTariff
from .config import Config
from .official_notes import OfficialNotesService
from .rules import load_notes
from .runlog import RunLog
from .tariff import Tariff, fmt_code, normalize_code

Answerer = Callable[[str], Awaitable[str]]


async def console_answerer(question: str) -> str:
    print(f"\n🤖 Agent 问：{question}")
    return input("你的回答（直接回车表示不知道）：").strip() or "不知道，请按最常见的情况假设，并写进 missing_info。"


async def no_more_info(question: str) -> str:
    """评测时使用：模拟用户给不出更多信息。"""
    return "没有更多信息了。请根据现有描述做出最合理的判断，把缺少的信息写进 missing_info，并相应降低置信度。"


class Step(BaseModel):
    code: str
    reason: str


class Classification(BaseModel):
    hs6: str
    hs6_desc: str
    path: str
    cn_code: str = ""
    cn_name: str = ""
    refund_rate: float | None = None
    conditions: str = ""
    vat_rate: str = ""
    refund_note: str = ""
    data_version: str = ""
    product_summary: str
    steps: list[Step]
    rules: list[str]
    confidence: float
    need_review: bool
    missing_info: list[str]
    alternatives: list[str]


@dataclass
class RunState:
    config: Config
    tariff: Tariff
    cn: CnTariff
    log: RunLog
    answerer: Answerer = no_more_info
    browsed: set[str] = field(default_factory=set)
    answer: Classification | None = None
    finish_rejections: int = 0
    notes: OfficialNotesService | None = None  # 在线查询官方注释；None 表示不查
    notes_shown: set[str] = field(default_factory=set)  # 已经给 Agent 看过类注、章注的章


# 每段注释最多给模型看多少字。类注、章注里的排除条款和子目注释常在后半段，截短会直接导致归错
# （评测里第 39 章章注 1.5 万字只给了 4000 字，「本章不包括第九十章的物品」就被截掉了），所以给得足够长。
NOTES_LIMIT = 20000


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n…（后面还有 {len(text) - n} 字，已省略）"


def _short(text: str, n: int = 110) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


class Toolbox:
    def __init__(self, state: RunState):
        self.s = state

    @property
    def t(self) -> Tariff:
        return self.s.tariff

    def search(self, keywords: str, chapter: str = "") -> str:
        chapter = normalize_code(chapter)[:2] if chapter else ""
        hits = self.t.search(keywords, k=15, chapter=chapter)
        self.s.log.action("search", {"keywords": keywords, "chapter": chapter}, ok=bool(hits))
        if not hits:
            return "没有检索到结果。换一组英文关键词试试（用税则里的说法：材质、功能、用途），或者从 browse 类目逐层找。"
        lines = [f"检索「{keywords}」的结果（按相关度排序，只是候选，要用 browse 核对品目条文和同级子目）："]
        for code, _ in hits:
            node = self.t.get(code)
            heading = self.t.get(node.parent)
            lines.append(f"- {fmt_code(code)} {_short(node.desc)}  ‖ 品目 {heading.code}: {_short(heading.desc, 70)}")
        return "\n".join(lines)

    async def browse(self, code: str = "") -> str:
        code = normalize_code(code)
        if code in ("", "TOTAL"):
            self.s.log.action("browse", {"code": "ALL"})
            lines = ["HS 共 21 类："]
            for sec in self.t.sections:
                node = self.t.nodes[sec]
                chapters = ",".join(node.children)
                lines.append(f"- 第{sec}类 {_short(node.desc, 90)}（章：{chapters}）")
            return "\n".join(lines)
        node = self.t.get(code)
        if not node:
            self.s.log.action("browse", {"code": code}, ok=False)
            return f"税则里没有 {code}。品目是 4 位、子目是 6 位、章是 2 位、类是罗马数字。"
        self.s.browsed.add(node.code)
        self.s.log.action("browse", {"code": node.code})
        lines = [f"位置：{self.t.path_text(node.code)}", f"{self.t.label(node)} 条文：{node.desc}"]
        if node.children:
            kind = {1: "章", 2: "品目", 4: "子目"}[node.level]
            lines.append(f"下级{kind}（同级之间比较，选最符合的一个）：")
            for c in node.children:
                lines.append(f"- {self.t.label(self.t.nodes[c])} {self.t.nodes[c].desc}")
        else:
            parent = self.t.get(node.parent)
            lines.append(f"这是 6 位子目（最末级）。同一品目 {parent.code} 下的全部子目：")
            for c in parent.children:
                mark = " ← 当前" if c == node.code else ""
                lines.append(f"- {fmt_code(c)} {self.t.nodes[c].desc}{mark}")
            self.s.browsed.add(parent.code)
        if node.level in (1, 2):
            notes = load_notes(self.s.config.notes_dir, node.section, node.code if node.level == 2 else "")
            lines.append(f"\n注释：\n{notes}" if notes else "\n（本地没有这一类/章的注释文本，凭条文和总规则判断，并在置信度上体现。）")
        if self.s.notes and node.level == 2 and node.children:
            # 类注、章注在每个品目的「详细」里都有，用本章第一个品目去查
            lines.append(await self._official_notes(node.children[0], chapter_only=True))
        if self.s.notes and node.level == 4:
            lines.append(await self._official_notes(node.code))
        if node.level == 6 and self.s.cn.loaded:
            lines.append("\n" + self.cn_codes(node.code, log=False))
        return "\n".join(lines)

    async def _official_notes(self, heading: str, chapter_only: bool = False) -> str:
        if chapter_only:
            notes, source = await self.s.notes.get_chapter(heading[:2], heading)
        else:
            notes, source = await self.s.notes.get(heading)
        if not notes and self.s.notes.offline:
            return ""  # 没开在线查询，本地也没导入，就不提了
        self.s.log.action("official_notes", {"heading": heading, "source": source}, ok=bool(notes))
        if not notes:
            return f"\n（官方注释暂时查不到：{source}。凭条文和总规则判断，并在置信度上体现。）"
        chapter = heading[:2]
        parts = [f"\n【海关官方注释，来源：{source}】"]
        if chapter not in self.s.notes_shown:
            self.s.notes_shown.add(chapter)
            section_key = "类注:" + notes.section_notes[:200]  # 同一类的几章共用类注，只给一次
            if notes.section_notes and section_key not in self.s.notes_shown:
                self.s.notes_shown.add(section_key)
                parts.append("类注：\n" + _clip(notes.section_notes, NOTES_LIMIT))
            elif notes.section_notes:
                parts.append("（本类的类注前面已经给过。）")
            if notes.chapter_notes:
                parts.append("章注：\n" + _clip(notes.chapter_notes, NOTES_LIMIT))
        elif chapter_only:
            parts.append("（本章的类注、章注前面已经给过。）")
        if not chapter_only and notes.heading_notes:
            parts.append(f"品目 {heading} 注释：\n" + _clip(notes.heading_notes, NOTES_LIMIT))
        return "\n".join(parts)

    def cn_codes(self, hs6: str, log: bool = True) -> str:
        hs6 = normalize_code(hs6)
        if log:
            self.s.log.action("cn_codes", {"hs6": hs6})
        if not self.s.cn.loaded:
            return "没有导入中国税则数据，只能给出 6 位编码。"
        items = self.s.cn.under(hs6)
        if not items:
            return f"中国税则数据里没有 {fmt_code(hs6)} 下的 8/10 位编码。"
        lines = [f"{fmt_code(hs6)} 下的中国税则编码："]
        lines += [f"- {fmt_code(it.code)} {it.name}" for it in items]
        return "\n".join(lines)

    async def ask_human(self, question: str) -> str:
        self.s.log.questions += 1
        reply = await self.s.answerer(question)
        self.s.log.action("ask_human", {"question": question}, note=reply)
        return f"用户回答：{reply}"

    def finish(
        self,
        hs6: str,
        cn_code: str,
        product_summary: str,
        steps: list[Step],
        rules: list[str],
        confidence: float,
        missing_info: list[str],
        alternatives: list[str],
    ) -> str:
        problem = self._check(hs6, cn_code)
        if problem:
            self.s.finish_rejections += 1
            self.s.log.action("finish", {"hs6": hs6, "cn_code": cn_code}, ok=False, note=problem)
            return f"提交被拒绝：{problem}"
        node = self.t.get(hs6)
        item = self.s.cn.get(cn_code) if cn_code and self.s.cn.loaded else None
        confidence = max(0.0, min(1.0, confidence))
        alts = [normalize_code(a) for a in alternatives]
        self.s.answer = Classification(
            hs6=node.code,
            hs6_desc=node.desc,
            path=self.t.path_text(node.code),
            cn_code=item.code if item else "",
            cn_name=item.name if item else "",
            refund_rate=item.refund_rate if item else None,
            conditions=item.conditions if item else "",
            vat_rate=item.vat_rate if item else "",
            refund_note=item.note if item else "",
            data_version=item.version if item else "",
            product_summary=product_summary,
            steps=steps,
            rules=rules,
            confidence=confidence,
            need_review=confidence < self.s.config.review_threshold,
            missing_info=missing_info,
            alternatives=[a for a in alts if self.t.is_leaf(a) and a != node.code],
        )
        self.s.log.action("finish", {"hs6": node.code, "cn_code": cn_code, "confidence": confidence})
        return "已提交。"

    def _check(self, hs6: str, cn_code: str) -> str:
        code = normalize_code(hs6)
        if not self.t.is_leaf(code):
            return f"{hs6} 不是税则里存在的 6 位子目。请用 browse 找到具体的 6 位子目再提交。"
        heading = code[:4]
        if heading not in self.s.browsed and code not in self.s.browsed:
            return f"你还没有浏览过品目 {heading}。请先 browse('{heading}') 比较它下面所有同级子目，再决定。"
        if self.s.cn.loaded and self.s.cn.under(code):
            valid = {it.code for it in self.s.cn.under(code)}
            if normalize_code(cn_code) not in valid:
                return f"已导入中国税则，cn_code 必须是 {fmt_code(code)} 下的编码之一：" + "、".join(sorted(valid))
        return ""
