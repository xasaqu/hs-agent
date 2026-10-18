"""中国 8/10 位商品编码、出口退税率和监管条件：从本地 CSV 读取，由程序查表，不让模型凭记忆回答。

数据由 import_refund.py 从国家税务总局「出口退税率文库」生成，每次文库更新后重新导入即可。
CSV 列：code（8 或 10 位数字）, name（商品名称）, refund_rate（出口退税率，百分数，如 13）,
       vat_rate（增值税征税率，可能是「13/9」多档）, conditions（监管条件代码，可空）,
       note（分档、后续调整等说明）, version（数据版本）, source（数据来源）
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .tariff import normalize_code

COLUMNS = ["code", "name", "refund_rate", "vat_rate", "conditions", "note", "version", "source"]


@dataclass
class CnItem:
    code: str
    name: str
    refund_rate: float | None
    conditions: str
    version: str
    source: str
    vat_rate: str = ""
    note: str = ""


class CnTariff:
    def __init__(self, path: Path):
        self.items: dict[str, CnItem] = {}
        self.path = path
        if not path.is_file():
            return
        for row in csv.DictReader(path.open(encoding="utf-8-sig")):
            code = normalize_code(row.get("code", ""))
            if len(code) not in (8, 10):
                continue
            rate = (row.get("refund_rate") or "").strip().rstrip("%")
            self.items[code] = CnItem(
                code=code,
                name=(row.get("name") or "").strip(),
                refund_rate=float(rate) if rate else None,
                conditions=(row.get("conditions") or "").strip(),
                version=(row.get("version") or "").strip(),
                source=(row.get("source") or "").strip(),
                vat_rate=(row.get("vat_rate") or "").strip(),
                note=(row.get("note") or "").strip(),
            )

    @property
    def loaded(self) -> bool:
        return bool(self.items)

    def under(self, hs6: str) -> list[CnItem]:
        hs6 = normalize_code(hs6)
        return sorted((it for it in self.items.values() if it.code.startswith(hs6)), key=lambda it: it.code)

    def get(self, code: str) -> CnItem | None:
        return self.items.get(normalize_code(code))
