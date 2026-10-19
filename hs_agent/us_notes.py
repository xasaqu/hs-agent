"""从美国税则第 99 章注释（PDF 转成的文字）里提取两份清单：

- 301 清单：note 20（9903.88.01/.02/.03/.15，即 List 1~4A）和 note 31（9903.91.xx，四年期复审上调）里列出的税号；
- 9903.05.31 的豁免清单：note 52 里对所有国家适用的豁免（9903.05.86 农产品等、.88 民用航空器、.89 制药用途）。

note 20 里的排除清单（9903.88.xx 的 exclusions）是按商品描述写的，没法按税号判断，不提取。

用法：pdftotext -layout chapter99.pdf ch99.txt && python -m hs_agent.us_notes ch99.txt
结果写到 hs_agent/data/us_301_lists.csv 和 us_exempt_9903_05_31.csv。
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

CODE = re.compile(r"(?<![\d.])(\d{4}\.\d{2}\.\d{2}(?:\d{2})?)(?![\d.]*\d)")
START_301 = re.compile(r"Heading (9903\.(?:88|91)\.\d\d) applies to")
START_EXEMPT = re.compile(r"As provided in heading (9903\.05\.(?:8[5-9]|9[0-2])), the (?:additional )?duties imposed by\s+headings?\s+9903\.05\.20")
# 下一个分项的开头：(c) For the purposes… / (t) Heading… / (i) The U.S. Trade Representative…
NEXT = re.compile(r"^\s{0,20}(?:\d{1,2}\.\s+)?\((?:[a-z]{1,4})\)\s+(?:\((?:[a-z]{1,4})\)\s+)?"
                  r"(For the purposes|For purposes|Heading|The U\.S\. Trade|As provided|Except|Notwithstanding)")
NEW_NOTE = re.compile(r"^\d{1,2}\.\s")

LIST_NAMES = {"9903.88.01": "List 1", "9903.88.02": "List 2", "9903.88.03": "List 3", "9903.88.15": "List 4A"}
EXEMPT_NOTES = {
    "9903.05.86": ("", 0),
    "9903.05.88": ("仅限民用航空器及其发动机、零部件（需符合一般注释 6）", 1),
    "9903.05.89": ("仅限制药用途", 1),
}
SKIP_301 = {"9903.88.16"}  # List 4B，已暂停


def segments(lines: list[str], start: re.Pattern) -> list[tuple[str, int, int]]:
    out = []
    i = 0
    while i < len(lines):
        m = start.search(lines[i])
        if not m:
            i += 1
            continue
        heading, j = m.group(1), i + 1
        while j < len(lines) and not NEXT.match(lines[j]) and not NEW_NOTE.match(lines[j]):
            j += 1
        out.append((heading, i, j))
        i = j
    return out


def codes_in(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        if "Harmonized Tariff Schedule" in line:
            continue
        for c in CODE.findall(line):
            digits = c.replace(".", "")
            if not digits.startswith(("98", "99")):
                found.append(digits)
    return found


def parse(text: str) -> tuple[list[dict], list[dict]]:
    lines = text.splitlines()
    s301, seen = [], set()
    for heading, a, b in segments(lines, START_301):
        if heading in SKIP_301:
            continue
        for code in codes_in(lines[a:b]):
            if (code, heading) not in seen:
                seen.add((code, heading))
                s301.append({"code": code, "heading": heading, "list": LIST_NAMES.get(heading, "四年期复审")})
    exempt, seen = [], set()
    for heading, a, b in segments(lines, START_EXEMPT):
        if heading not in EXEMPT_NOTES:
            continue  # .87 是按商品描述列的，.90 是 232 金属（关税计算里单独处理）
        note, cond = EXEMPT_NOTES[heading]
        for code in codes_in(lines[a:b]):
            if code not in seen:
                seen.add(code)
                exempt.append({"code": code, "heading": heading, "note": note, "conditional": cond})
    return s301, exempt


def write(rows: list[dict], path: Path, fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    from .config import Config
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    config = Config()
    s301, exempt = parse(Path(sys.argv[1]).read_text(encoding="utf-8"))
    write(s301, config.us_301_file, ["code", "heading", "list"])
    write(exempt, config.us_exempt_file, ["code", "heading", "note", "conditional"])
    by = {}
    for r in s301:
        by[r["heading"]] = by.get(r["heading"], 0) + 1
    print("301 清单：" + "，".join(f"{h} {n} 个" for h, n in sorted(by.items())))
    print(f"9903.05.31 豁免清单：{len(exempt)} 个（其中有条件的 {sum(r['conditional'] for r in exempt)} 个）")


if __name__ == "__main__":
    main()
