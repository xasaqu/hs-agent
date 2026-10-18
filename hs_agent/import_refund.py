"""把国家税务总局「出口退税率文库」（CMCODE 系列，DBF 格式）转换成 hs_agent/data/cn_tariff.csv。

文库每个编码按执行时间分多条记录（起止日期），这里取「指定日期当天有效」的那一条；
已经公布、但还没开始执行的调整，写进 note 提醒。
同一个 10 位编码按征税率拆成 9%、13% 两档的（11 位编码），也合并进 note。

用法（先把压缩包解压，得到 cmcode.dbf、stdcm.DBF）：
  pip install dbfread
  python -m hs_agent.import_refund 解压后的文件夹 --version 2026C
"""

from __future__ import annotations

import argparse
import csv
import datetime
from collections import defaultdict
from pathlib import Path

from .config import DATA_DIR

COLUMNS = ["code", "name", "refund_rate", "vat_rate", "conditions", "note", "version", "source"]


def _find(folder: Path, name: str) -> Path | None:
    for p in folder.iterdir():
        if p.name.lower() == name.lower():
            return p
    return None


def _rate(value) -> str:
    """数字转成「13」这样的写法；文库里征税率可能是「13,9」这种多档，原样转成「13/9」。"""
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):g}"
    except ValueError:
        return str(value).strip().replace(",", "/")


def convert(folder: Path, out: Path, version: str, on: datetime.date) -> dict:
    from dbfread import DBF  # 只有导入时才需要

    cm = _find(folder, "cmcode.dbf")
    if not cm:
        raise SystemExit(f"在 {folder} 里没找到 cmcode.dbf，请先解压退税率文库。")
    std = _find(folder, "stdcm.dbf")
    full_names = {}
    if std:  # cmcode 里的名称被截断成 10 个字，标准名称表里是完整的
        full_names = {r["CODE"].strip(): r["NAME"].strip() for r in DBF(std, encoding="gbk", char_decode_errors="replace")}

    current: dict[str, dict] = {}
    upcoming: dict[str, list[dict]] = defaultdict(list)
    for r in DBF(cm, encoding="gbk", char_decode_errors="replace"):
        code = (r["CODE"] or "").strip()
        start, end = r["ST_DATE"], r["END_DATE"]
        if not code.isdigit() or not start:
            continue
        if start <= on and (end is None or end >= on):
            current[code] = r
        elif start > on:
            upcoming[code].append(r)

    splits: dict[str, list[dict]] = defaultdict(list)  # 10 位编码 → 按征税率拆分的 11 位记录
    for code, r in current.items():
        if len(code) == 11:
            splits[code[:10]].append(r)

    rows = []
    for code, r in sorted(current.items()):
        if len(code) not in (8, 10):
            continue
        notes = []
        if splits.get(code):
            parts = [f"征税率 {_rate(s['ZSSL_SET'])}% 的退 {_rate(s['TSL'])}%" for s in sorted(splits[code], key=lambda s: s["CODE"])]
            notes.append("按征税率分档：" + "；".join(parts))
        for u in sorted(upcoming.get(code, []), key=lambda u: u["ST_DATE"]):
            notes.append(f"{u['ST_DATE']:%Y-%m-%d} 起退税率调整为 {_rate(u['TSL'])}%")
        rows.append({
            "code": code,
            "name": full_names.get(code) or (r["NAME"] or "").strip(),
            "refund_rate": _rate(r["TSL"]),
            "vat_rate": _rate(r["ZSSL_SET"]),
            "conditions": "",
            "note": "；".join(notes),
            "version": f"{version}（{on:%Y-%m-%d} 有效）",
            "source": "国家税务总局出口退税率文库",
        })

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return {
        "rows": len(rows),
        "eight": sum(1 for r in rows if len(r["code"]) == 8),
        "ten": sum(1 for r in rows if len(r["code"]) == 10),
        "split": sum(1 for c in splits if c in current),
        "upcoming": sum(1 for r in rows if "起退税率调整" in r["note"]),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="导入出口退税率文库")
    p.add_argument("folder", help="解压后的文件夹（包含 cmcode.dbf）")
    p.add_argument("--version", default="", help="文库版本，如 2026C")
    p.add_argument("--date", default="", help="取哪一天有效的税率，默认今天，格式 2026-10-18")
    p.add_argument("--out", default=str(DATA_DIR / "cn_tariff.csv"))
    args = p.parse_args()
    on = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    stats = convert(Path(args.folder), Path(args.out), args.version, on)
    print(f"已写入 {args.out}：共 {stats['rows']} 个编码（8 位 {stats['eight']}，10 位 {stats['ten']}），"
          f"其中 {stats['split']} 个按征税率分档，{stats['upcoming']} 个有已公布的后续调整。")


if __name__ == "__main__":
    main()
