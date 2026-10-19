"""美国进口税则（HTSUS）：从美国国际贸易委员会（USITC）官网下载，存成本地 JSON，按税号查税率和附加税。

中国编码和美国编码只有前 6 位通用。按 6 位子目列出美国的 8/10 位税号和税率，由用户选定具体税号。

数据来源：https://hts.usitc.gov （美国政府公开数据）。下载：
  python -m hs_agent.us_hts --download          # 逐章下载，约 100 次请求，每次间隔 1 秒
  python -m hs_agent.us_hts --import 文件.json   # 或者导入从官网手动导出的 JSON / CSV
  python -m hs_agent.us_hts 8516.60             # 查一个子目下的美国税号

附加税（Section 301 等）不写在税率栏里，而是写在脚注「See 9903.88.03.」，
具体加征多少在第 99 章对应税号的税率栏里，例如「The duty provided in the applicable subheading + 25%」。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import ssl
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .tariff import normalize_code

CH99_PDF_URL = "https://hts.usitc.gov/reststop/file?release=currentRelease&filename=Chapter%2099"
EXPORT_URL = "https://hts.usitc.gov/reststop/exportList?from={a}&to={b}&format=JSON&styles=false"
CH99_REF = re.compile(r"99\d\d\.\d\d\.\d\d")

# 第 99 章附加税号的类别（按前缀判断，覆盖不到的列为「其他」，让用户自己核对）
CH99_KINDS = [
    ("9903.88.", "301"),   # Section 301 对华 List 1~4A
    ("9903.91.", "301"),   # 2024 年四年期复审后上调的 301 税率
    ("9903.92.", "301"),
    ("9903.80.", "232"), ("9903.81.", "232"),  # 钢
    ("9903.85.", "232"),   # 铝
    ("9903.78.", "232"),   # 铜
    ("9903.76.", "232"),   # 木材、家具、橱柜
    ("9903.94.", "232"),   # 汽车及零部件
    ("9903.01.", "ieepa"), ("9903.02.", "ieepa"),  # IEEPA，2026-02-20 已被裁定无效
]


@dataclass
class Rate:
    """税率栏解析结果。从价部分是百分数；从量部分（¢/kg 等）单列，能换算的才换算。"""
    raw: str
    ad_valorem: float = 0.0
    specific: list[tuple[float, str]] = field(default_factory=list)  # (美元, 单位)，例如 (0.044, "kg")
    ok: bool = True  # False：看不懂的写法，需要人工核对

    @property
    def text(self) -> str:
        return self.raw or "（空）"


def parse_rate(text: str) -> Rate:
    raw = (text or "").strip()
    r = Rate(raw)
    if not raw or raw.lower().startswith("free"):
        return r
    rest = raw.lower()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", rest):
        r.ad_valorem += float(m.group(1))
    rest = re.sub(r"\d+(?:\.\d+)?\s*%", " ", rest)
    # 4.4¢/kg、$1.25/doz、25¢/kg on ...
    for m in re.finditer(r"(\$)?(\d+(?:\.\d+)?)\s*(¢)?\s*/\s*([a-z0-9]+)", rest):
        usd = round(float(m.group(2)) / 100, 6) if m.group(3) else float(m.group(2))
        r.specific.append((usd, m.group(4)))
    rest = re.sub(r"\$?\d+(?:\.\d+)?\s*¢?\s*/\s*[a-z0-9]+", " ", rest)
    leftover = re.sub(r"\b(plus|and|on|of|the|contents?|each|kg|\+)\b|[+;,.()]", " ", rest).strip()
    if leftover or (r.ad_valorem == 0 and not r.specific):
        r.ok = False  # 像「The rate applicable to ...」这类文字说明，需要人工看
    return r


def ch99_extra(text: str) -> float | None:
    """第 99 章税率栏里的加征幅度：'The duty provided in the applicable subheading + 25%' → 25。"""
    t = (text or "").strip()
    m = re.search(r"(?:\+|\bplus)\s*(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return float(m.group(1))
    if t.lower().startswith("the duty provided in the applicable subheading"):
        return 0.0  # 只适用原税率，常见于排除清单
    return None


def ch99_kind(ref: str) -> str:
    for prefix, kind in CH99_KINDS:
        if ref.startswith(prefix):
            return kind
    return "other"


@dataclass
class Line:
    code: str          # 纯数字，4/6/8/10 位；没有编号的描述行为空
    indent: int
    desc: str
    general: str = ""
    special: str = ""
    other: str = ""
    units: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    additional: str = ""


def _key(d: dict, *names: str):
    low = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in d.items()}
    for n in names:
        if n in low:
            return low[n]
    return None


def line_from_usitc(item: dict) -> Line:
    """USITC 导出的一行（JSON 字段或 CSV 表头都认）。"""
    code = normalize_code(str(_key(item, "htsno", "htsnumber", "code") or ""))
    notes = _key(item, "footnotes") or []
    if isinstance(notes, str):
        notes = [notes] if notes.strip() else []
    texts = []
    for n in notes:
        texts.append(str(n.get("value", "")) if isinstance(n, dict) else str(n))
    units = _key(item, "units", "unitofquantity") or []
    if isinstance(units, str):
        units = [u.strip() for u in re.split(r"[,;]", units) if u.strip()]
    try:
        indent = int(str(_key(item, "indent") or 0).strip() or 0)
    except ValueError:
        indent = 0
    return Line(
        code=code, indent=indent,
        desc=re.sub(r"<[^>]+>", "", str(_key(item, "description") or "")).strip(),
        general=str(_key(item, "general", "generalrateofduty") or "").strip(),
        special=str(_key(item, "special", "specialrateofduty") or "").strip(),
        other=str(_key(item, "other", "column2rateofduty") or "").strip(),
        units=[str(u) for u in units],
        footnotes=[t.strip() for t in texts if t.strip()],
        additional=str(_key(item, "additionalduties") or "").strip(),
    )


def read_usitc(data: bytes, filename: str = "") -> list[dict]:
    """官网导出的 JSON 或 CSV → 原始行（dict）。"""
    text = data.decode("utf-8-sig", errors="replace")
    if filename.lower().endswith(".csv") or not text.lstrip().startswith(("[", "{")):
        return list(csv.DictReader(io.StringIO(text)))
    obj = json.loads(text)
    if isinstance(obj, dict):
        for k in ("data", "results", "lines", "items"):
            if isinstance(obj.get(k), list):
                return obj[k]
        raise ValueError("JSON 里没有找到税则行（期望是一个列表）")
    return obj


@dataclass
class UsLine:
    """查询结果：一个可申报的税号，税率已按上级继承，附加税已从第 99 章查出。"""
    code: str
    desc_path: list[str]
    general: Rate
    units: list[str]
    refs: list[dict]        # [{ref, kind, extra, desc}]
    rate_from: str          # 税率写在哪一级（10 位行常常留空，沿用 8 位的）

    def public(self) -> dict:
        return {
            "code": fmt_us(self.code), "desc": " > ".join(self.desc_path[-3:]), "full_desc": " > ".join(self.desc_path),
            "general": self.general.text, "ad_valorem": self.general.ad_valorem,
            "specific": [{"usd": u, "unit": n} for u, n in self.general.specific], "rate_ok": self.general.ok,
            "units": self.units, "refs": self.refs, "rate_from": fmt_us(self.rate_from),
        }


def fmt_us(code: str) -> str:
    """'8516604060' → '8516.60.40.60'。"""
    if len(code) <= 4:
        return code
    parts = [code[:4], code[4:6], code[6:8], code[8:10]]
    return ".".join(p for p in parts if p)


class UsHts:
    def __init__(self, path: Path):
        self.path = path
        self.lines: list[Line] = []
        self.meta: dict = {}
        self.ch99: dict[str, Line] = {}
        if path.is_file():
            obj = json.loads(path.read_text(encoding="utf-8"))
            self.meta = {k: v for k, v in obj.items() if k != "lines"}
            self.lines = [Line(**d) for d in obj.get("lines", [])]
            self._index()

    @classmethod
    def from_lines(cls, lines: list[Line], meta: dict | None = None) -> "UsHts":
        self = cls.__new__(cls)
        self.path, self.lines, self.meta = Path(""), lines, meta or {}
        self._index()
        return self

    def _index(self) -> None:
        self.ch99 = {fmt_us(l.code): l for l in self.lines if l.code.startswith("99")}
        self.by_code = {l.code: i for i, l in enumerate(self.lines) if l.code}

    @property
    def loaded(self) -> bool:
        return bool(self.lines)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        obj = dict(self.meta, lines=[l.__dict__ for l in self.lines])
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def ancestors(self, i: int) -> list[Line]:
        """按缩进往上找上级（含没有编号的描述行），从上到下。"""
        me = self.lines[i]
        out, indent = [], me.indent
        for j in range(i - 1, -1, -1):
            l = self.lines[j]
            # 有编号的上级必须是本税号的前缀（官网的缩进对统计税号不完全可靠）
            if l.indent < indent and (not l.code or me.code.startswith(l.code)):
                out.append(l)
                indent = l.indent
                if indent == 0:
                    break
        return list(reversed(out))

    def ref_info(self, ref: str) -> dict:
        l = self.ch99.get(ref)
        extra = ch99_extra(l.general) if l else None
        return {"ref": ref, "kind": ch99_kind(ref), "extra": extra,
                "desc": (l.desc[:300] if l else "本地税则里没有这个第 99 章税号，请到官网核对")}

    def resolve(self, i: int) -> UsLine:
        l = self.lines[i]
        chain = self.ancestors(i) + [l]
        rate_line = next((x for x in reversed(chain) if x.general and x.code), l)
        refs, seen = [], set()
        for x in chain:
            for text in x.footnotes + [x.additional]:
                for ref in CH99_REF.findall(text or ""):
                    if ref not in seen:
                        seen.add(ref)
                        refs.append(self.ref_info(ref))
        units = next((x.units for x in reversed(chain) if x.units), [])
        return UsLine(l.code, [x.desc for x in chain if x.desc], parse_rate(rate_line.general), units, refs, rate_line.code)

    def under(self, prefix: str) -> list[UsLine]:
        """某个编码（6/8/10 位）下所有可申报的末级税号。"""
        prefix = normalize_code(prefix)
        out = []
        for i, l in enumerate(self.lines):
            if not l.code.startswith(prefix) or len(l.code) < 8:
                continue
            nxt = self.lines[i + 1] if i + 1 < len(self.lines) else None
            # 10 位统计税号都列出；8 位的只有下面没有 10 位税号时才列
            if len(l.code) == 8 and nxt and nxt.indent > l.indent and (not nxt.code or nxt.code.startswith(l.code)):
                continue
            out.append(self.resolve(i))
        return out

    def get(self, code: str) -> UsLine | None:
        code = normalize_code(code)
        i = self.by_code.get(code)
        if i is None and len(code) == 10 and code.endswith("00"):  # 8 位已是末级时，统计后缀常写成 00
            i = self.by_code.get(code[:8])
        return self.resolve(i) if i is not None else None


# ---------- 下载和导入 ----------


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # openai 的依赖里通常就有；Mac 上官方 Python 自带证书有时不全
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch(url: str, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hs-agent/1.0 (tariff lookup)", "Accept": "application/json"})
    for k in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r:
                return r.read()
        except Exception:
            if k == retries - 1:
                raise
            time.sleep(2 * (k + 1))
    return b""


def build(raw: list[dict], source: str) -> UsHts:
    lines, seen = [], set()
    for item in raw:
        l = line_from_usitc(item)
        key = (l.code, l.indent, l.desc[:80])
        if key in seen or not (l.code or l.desc):
            continue
        seen.add(key)
        lines.append(l)
    meta = {"source": source, "saved": time.strftime("%Y-%m-%d %H:%M"), "count": len(lines)}
    return UsHts.from_lines(lines, meta)


def download(out: Path, interval: float = 1.0, raw_dir: Path | None = None) -> UsHts:
    raw: list[dict] = []
    empty = []
    chapters = [c for c in range(1, 100) if c != 77]  # 第 77 章空缺
    for n, ch in enumerate(chapters, 1):
        url = EXPORT_URL.format(a=f"{ch:02d}00", b=f"{ch:02d}99")
        try:
            data = fetch(url)
            rows = read_usitc(data)
        except Exception as e:
            print(f"  第 {ch:02d} 章下载失败：{e}", flush=True)
            empty.append(ch)
            continue
        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"ch{ch:02d}.json").write_bytes(data)
        if not rows:
            empty.append(ch)
        raw.extend(rows)
        print(f"[{n}/{len(chapters)}] 第 {ch:02d} 章：{len(rows)} 行", flush=True)
        if n == 1 and rows:
            print(f"  字段：{', '.join(rows[0].keys())}", flush=True)
        time.sleep(interval)
    hts = build(raw, "hts.usitc.gov exportList")
    if empty:
        hts.meta["missing_chapters"] = empty
        print(f"注意：这些章没有下载到数据：{empty}。可以再运行一次，或到官网手动导出后用 --import 导入。")
    hts.save(out)
    return hts


def chapter_blocks(lines: list[Line]) -> dict[str, list[Line]]:
    """按章分组，保持原来的顺序（没有编号的描述行跟着它前面的章走）。"""
    out: dict[str, list[Line]] = {}
    ch = ""
    for l in lines:
        if l.code:
            ch = l.code[:2]
        out.setdefault(ch, []).append(l)
    return out


def merge_chapters(old: list[Line], new: list[Line]) -> list[Line]:
    blocks = chapter_blocks(old)
    blocks.update(chapter_blocks(new))
    return [l for ch in sorted(blocks) for l in blocks[ch]]


def main() -> None:
    from .config import Config
    config = Config()
    p = argparse.ArgumentParser(description="美国进口税则（HTSUS）下载和查询")
    p.add_argument("code", nargs="?", help="查询的编码，例如 8516.60 或 8516.60.40")
    p.add_argument("--download", action="store_true", help="从 hts.usitc.gov 逐章下载")
    p.add_argument("--import", dest="import_", nargs="+", metavar="FILE", help="导入官网导出的 JSON / CSV（可以多个）")
    p.add_argument("--ch99-pdf", action="store_true", help="下载第 99 章 PDF（含 301 清单和 12.5%% 豁免清单的注释）")
    p.add_argument("--interval", type=float, default=1.0, help="下载时每次请求的间隔秒数")
    args = p.parse_args()
    out = config.us_hts_file
    if args.ch99_pdf:
        out_pdf = config.data_dir / "us_hts_raw" / "chapter99.pdf"
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        data = fetch(CH99_PDF_URL)
        if not data.startswith(b"%PDF"):
            sys.exit(f"下载到的不是 PDF（前 200 字节：{data[:200]!r}）")
        out_pdf.write_bytes(data)
        print(f"已保存 {len(data) // 1024} KB 到 {out_pdf}")
    if args.download:
        hts = download(out, args.interval, raw_dir=config.data_dir / "us_hts_raw")
        print(f"已保存 {hts.meta['count']} 行到 {out}")
    elif args.import_:
        raw = []
        for f in args.import_:
            raw.extend(read_usitc(Path(f).read_bytes(), f))
        hts = build(raw, "手动导入：" + "、".join(Path(f).name for f in args.import_))
        old = UsHts(out)
        if old.loaded:  # 和已有数据合并：导入的章整章替换，其余章保留
            hts.lines = merge_chapters(old.lines, hts.lines)
            hts._index()
        hts.save(out)
        print(f"已保存 {len(hts.lines)} 行到 {out}")
    if args.code:
        hts = UsHts(out)
        if not hts.loaded:
            sys.exit("还没有美国税则数据，请先运行 python -m hs_agent.us_hts --download")
        for u in hts.under(args.code):
            d = u.public()
            extra = "；".join(f"{r['ref']}（{r['kind']} +{r['extra']}%）" if r["extra"] is not None else r["ref"] for r in d["refs"])
            print(f"{d['code']:16} {d['general']:12} {extra}\n    {d['desc'][:120]}")


if __name__ == "__main__":
    main()
