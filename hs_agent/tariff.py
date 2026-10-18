"""HS 税则树：类 → 章（2 位）→ 品目（4 位）→ 子目（6 位），以及基于 BM25 的关键词检索。

数据来自 UN Comtrade 整理的 HS 2022 版本（ODC-PDDL 公共领域），条文为英文。
模型负责把中文商品描述转成英文关键词，检索和逐层浏览都在这棵树上进行，
所以最终编码一定是税则里真实存在的编码，不可能是模型编出来的。
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI",
         "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX", "XXI"]

STOPWORDS = {
    "a", "an", "and", "or", "of", "the", "for", "in", "on", "to", "with", "by", "as", "its", "their",
    "other", "than", "whether", "not", "nec", "n.e.c", "excluding", "including", "kind", "used", "item",
    "heading", "no", "be", "is", "are", "which", "such", "but", "thereof", "from", "into", "this", "that",
}


def stem(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    for suffix in ("ches", "shes", "xes"):
        if word.endswith(suffix):
            return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())
    out = []
    for w in words:
        if w in STOPWORDS:
            continue
        out.append(stem(w))
        if "-" in w:  # lithium-ion 同时索引 lithium、ion
            out.extend(stem(p) for p in w.split("-") if p and p not in STOPWORDS)
    return out


def normalize_code(code: str) -> str:
    """'8516.60' / '8516 60' / '851660' → '851660'；类号（罗马数字）原样返回。"""
    code = code.strip().upper()
    if code in ROMAN or code in ("", "TOTAL"):
        return code
    return re.sub(r"[^0-9]", "", code)


def fmt_code(code: str) -> str:
    """'851660' → '8516.60'，'8516601000' → '8516.60.1000'。"""
    if len(code) <= 4:
        return code
    if len(code) == 6:
        return f"{code[:4]}.{code[4:]}"
    return f"{code[:4]}.{code[4:6]}.{code[6:]}"


@dataclass
class Node:
    code: str
    desc: str
    parent: str
    level: int  # 1=类 2=章 4=品目 6=子目
    section: str
    children: list[str] = field(default_factory=list)


class Tariff:
    def __init__(self, hs_file: Path, sections_file: Path):
        self.nodes: dict[str, Node] = {}
        for row in csv.DictReader(sections_file.open(encoding="utf-8-sig")):
            sec = row["section"].strip()
            self.nodes[sec] = Node(sec, row["name"].strip(), "TOTAL", 1, sec)
        self.sections = [s for s in ROMAN if s in self.nodes]
        for row in csv.DictReader(hs_file.open(encoding="utf-8-sig")):
            level = int(row["level"])
            if level not in (2, 4, 6):
                continue
            code, parent = row["hscode"].strip(), row["parent"].strip()
            if code.startswith("99"):  # Comtrade 自加的「未分类商品」，不是 HS 正式章节
                continue
            if level == 2:
                parent = row["section"].strip()
            self.nodes[code] = Node(code, row["description"].strip(), parent, level, row["section"].strip())
        for node in self.nodes.values():
            if node.level > 1 and node.parent in self.nodes:
                self.nodes[node.parent].children.append(node.code)
        for node in self.nodes.values():
            node.children.sort(key=lambda c: (len(c), c))
        self._build_index()

    # ---------- 树 ----------

    def get(self, code: str) -> Node | None:
        return self.nodes.get(normalize_code(code))

    def is_leaf(self, code: str) -> bool:
        node = self.get(code)
        return bool(node) and node.level == 6

    def path(self, code: str) -> list[Node]:
        out = []
        node = self.get(code)
        while node:
            out.append(node)
            node = self.nodes.get(node.parent)
        return list(reversed(out))

    def path_text(self, code: str) -> str:
        return " > ".join(f"{self.label(n)} {n.desc[:60]}" for n in self.path(code))

    @staticmethod
    def label(node: Node) -> str:
        if node.level == 1:
            return f"第{node.code}类"
        if node.level == 2:
            return f"第{node.code}章"
        return fmt_code(node.code)

    # ---------- 检索 ----------

    def _build_index(self) -> None:
        """只索引 6 位子目；每条文档 = 子目条文 + 所属品目条文（品目权重较低）。"""
        self.docs: list[str] = []
        self.doc_tf: list[Counter] = []
        for code, node in self.nodes.items():
            if node.level != 6:
                continue
            heading = self.nodes.get(node.parent)
            tokens = tokenize(node.desc) * 2 + (tokenize(heading.desc) if heading else [])
            self.docs.append(code)
            self.doc_tf.append(Counter(tokens))
        self.doc_len = [sum(tf.values()) for tf in self.doc_tf]
        self.avg_len = sum(self.doc_len) / max(len(self.doc_len), 1)
        df: Counter = Counter()
        for tf in self.doc_tf:
            df.update(tf.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 15, chapter: str = "") -> list[tuple[str, float]]:
        terms = tokenize(query)
        if not terms:
            return []
        k1, b = 1.5, 0.75
        scores = []
        for i, tf in enumerate(self.doc_tf):
            code = self.docs[i]
            if chapter and not code.startswith(chapter):
                continue
            s = 0.0
            for t in terms:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * self.doc_len[i] / self.avg_len))
            if s > 0:
                scores.append((code, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:k]
