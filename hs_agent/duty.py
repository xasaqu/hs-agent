"""美国进口关税：按税号逐层计算，每层写明税率、金额、依据和生效日期。

计税基础是成交价（大致相当于 FOB，不含国际运费和保险）。层次：
  MFN 基础税率 → Section 301（税则脚注指向的第 99 章税号）→ 9903.05.31 新加征 12.5%（有豁免清单，
  232 商品不叠加）→ Section 232（用户填税率）→ 反倾销 / 反补贴（用户填税率）。
整票再加 MPF（货物处理费）和 HMF（港口维护费，只对海运）。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .tariff import normalize_code
from .us_hts import UsHts, fmt_us


def _d(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def in_effect(entry: dict, day: date) -> bool:
    start, end = _d(entry.get("from", "")), _d(entry.get("to", ""))
    return (not start or day >= start) and (not end or day <= end)


class Policy:
    def __init__(self, path: Path, exempt_path: Path | None = None, s301_path: Path | None = None):
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        self.exempt: dict[str, tuple[str, bool]] = {}  # 税号 → (说明, 是否有条件)
        if exempt_path and exempt_path.is_file():
            for row in csv.DictReader(exempt_path.open(encoding="utf-8-sig")):
                code = normalize_code(row.get("code", ""))
                if len(code) >= 4:
                    self.exempt[code] = ((row.get("note") or "").strip(), (row.get("conditional") or "0").strip() == "1")
        # 301 清单（从第 99 章注释提取，见 us_notes.py）：税号 → 第 99 章税号
        self.s301: dict[str, list[str]] = {}
        if s301_path and s301_path.is_file():
            for row in csv.DictReader(s301_path.open(encoding="utf-8-sig")):
                self.s301.setdefault(normalize_code(row["code"]), []).append(row["heading"])

    def s301_match(self, code: str) -> list[str]:
        """8 位子目在清单里，或 10 位统计税号单独列在清单里。"""
        out = []
        for key in {code[:10], code[:8]}:
            for h in self.s301.get(key, []):
                if h not in out:
                    out.append(h)
        return out

    def pick(self, key: str, day: date) -> dict | None:
        return next((e for e in self.data.get(key, []) if in_effect(e, day)), None)

    def exempt_match(self, code: str) -> str | None:
        """豁免清单按前缀匹配（清单里可能是 8 位或 10 位）。"""
        for n in (10, 8, 6, 4):
            if len(code) >= n and code[:n] in self.exempt:
                return code[:n]
        return None

    def s232_hint(self, code: str) -> str:
        hints = self.data.get("s232_hints", {})
        return hints.get(code[:4]) or hints.get(code[:2]) or ""


@dataclass
class Layer:
    name: str
    rate: float          # 百分数；从量税为 0
    amount: float        # 美元
    basis: str           # 怎么算的
    source: str = ""

    def public(self) -> dict:
        return {"name": self.name, "rate": round(self.rate, 4), "amount": round(self.amount, 2),
                "basis": self.basis, "source": self.source}


@dataclass
class DutyResult:
    code: str
    layers: list[Layer] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(l.amount for l in self.layers)

    @property
    def rate(self) -> float:
        return sum(l.rate for l in self.layers)

    def public(self) -> dict:
        return {"code": fmt_us(self.code), "layers": [l.public() for l in self.layers],
                "total": round(self.total, 2), "rate": round(self.rate, 4), "warnings": self.warnings}


SPECIFIC_QTY = {"kg": "kg", "no": "unit", "each": "unit", "pcs": "unit", "pc": "unit", "doz": "doz", "pr": "unit", "prs": "unit"}


def line_duty(hts: UsHts, policy: Policy, code: str, value_usd: float, qty: float, net_kg: float, arrival: date,
              s232_rate: float = 0, adcvd_rate: float = 0, s301_rate: float | None = None,
              exempt: bool | None = None) -> DutyResult:
    """一个税号的关税。value_usd 是这一行的总成交价；s301_rate / exempt 为 None 时按税则和豁免清单判断。"""
    code = normalize_code(code)
    res = DutyResult(code)
    u = hts.get(code) if hts.loaded else None
    if not u:
        res.warnings.append(f"美国税则里没有 {fmt_us(code)}，基础税率按 0 计算，请核对税号。" if hts.loaded
                            else "还没有导入美国税则，基础税率和 301 按 0 计算。")
    else:
        g = u.general
        amount = value_usd * g.ad_valorem / 100
        basis = f"成交价 ${value_usd:,.2f} × {g.ad_valorem:g}%"
        for usd, unit in g.specific:
            kind = SPECIFIC_QTY.get(unit)
            n = net_kg if kind == "kg" else qty / 12 if kind == "doz" else qty if kind == "unit" else None
            if n is None:
                res.warnings.append(f"从量税「{usd:g} 美元/{unit}」的单位没法自动换算，没有计入，请手算。")
                continue
            amount += usd * n
            basis += f" + {usd:g} 美元/{unit} × {n:g}"
            if kind == "kg":
                res.warnings.append("从量税按毛重估算；按净重计会略低。")
        if not g.ok:
            res.warnings.append(f"基础税率写法是「{g.raw}」，没能完全解析，请人工核对。")
        res.layers.append(Layer("MFN 基础税率", g.ad_valorem, amount, f"{g.raw or 'Free'}：{basis}",
                                f"HTSUS {fmt_us(u.rate_from)}"))

        r301 = [r for r in u.refs if r["kind"] == "301"]
        for h in policy.s301_match(code):
            if all(r["ref"] != h for r in r301):
                r301.append(hts.ref_info(h))
        if s301_rate is None:
            known = [r for r in r301 if r["extra"] is not None]
            if known:
                best = max(known, key=lambda r: r["extra"])
                s301_rate = best["extra"]
                src = best["ref"]
                if len(r301) > 1:
                    res.warnings.append("这个税号同时出现在多个 301 清单里（"
                                        + "、".join(f"{r['ref']} +{r['extra']:g}%" for r in known) + "），按最高的计算。")
                res.warnings.append("301 的排除清单是按商品描述列的，这里没有判断；如果商品确实在排除清单内，把 301 税率手动填 0。")
            else:
                s301_rate, src = 0.0, ""
                if r301:
                    res.warnings.append("301 税号在本地税则里查不到税率：" + "、".join(r["ref"] for r in r301))
                elif not policy.s301:
                    res.warnings.append("还没有导入 301 清单（第 99 章注释），301 按 0 计算。")
        else:
            src = "手动填写"
        if s301_rate:
            res.layers.append(Layer("Section 301", s301_rate, value_usd * s301_rate / 100,
                                    f"成交价 × {s301_rate:g}%", src))
        for r in u.refs:
            if r["kind"] == "ieepa":
                res.warnings.append(f"脚注里的 {r['ref']} 是 IEEPA 关税，已被裁定无效，不计。")
            elif r["kind"] == "other":
                res.warnings.append(f"脚注里还有 {r['ref']}，不在已知类别里，请核对是否加征：{r['desc'][:80]}")
            elif r["kind"] == "232" and not s232_rate:
                res.warnings.append(f"脚注指向 232 税号 {r['ref']}，如果适用请填写 232 税率。")

    hint = policy.s232_hint(code)
    if hint and not s232_rate:
        res.warnings.append(f"第 {code[:4]} 品目（{hint}）可能涉及 Section 232，请核对；适用的话填写 232 税率。")
    if s232_rate:
        res.layers.append(Layer("Section 232", s232_rate, value_usd * s232_rate / 100, f"成交价 × {s232_rate:g}%（手动填写）"))

    extra = policy.pick("cn_extra", arrival)
    if extra:
        line99 = hts.ch99.get(extra["heading"]) if hts.loaded else None
        rate = extra["rate"]
        if s232_rate:
            res.warnings.append(f"已按 232 计征，{extra['heading']} 的 {rate:g}% 不叠加。")
        else:
            hit = policy.exempt_match(code)
            note, cond = policy.exempt.get(hit, ("", False)) if hit else ("", False)
            if exempt is None:
                exempt = bool(hit) and not cond
                if cond:
                    res.warnings.append(f"在 {extra['heading']} 的有条件豁免清单里（{note}）；按不豁免计算，符合条件的可以手动标为豁免。")
                if not policy.exempt:
                    res.warnings.append(f"{extra['heading']} 的豁免清单还没有导入，按不豁免计算；在清单里的商品实际不交这 {rate:g}%。")
            if exempt:
                res.warnings.append(f"在 {extra['heading']} 豁免清单内（{fmt_us(hit) if hit else '手动标记'}），不计 {rate:g}%。")
            else:
                res.layers.append(Layer(extra["name"], rate, value_usd * rate / 100,
                                        f"成交价 × {rate:g}%（{extra['from']} 起）",
                                        extra["heading"] + ("" if line99 else "，以政策文件为准")))
    elif arrival < date(2026, 7, 24):
        res.warnings.append("到港日早于 2026-07-24，本工具没有覆盖那之前的临时关税。")

    if adcvd_rate:
        res.layers.append(Layer("反倾销 / 反补贴", adcvd_rate, value_usd * adcvd_rate / 100, f"成交价 × {adcvd_rate:g}%（手动填写）"))
    return res


def entry_fees(policy: Policy, total_value: float, sea: bool, arrival: date) -> tuple[list[Layer], list[str]]:
    """整票的 MPF 和 HMF。"""
    layers, warnings = [], []
    mpf = policy.pick("mpf", arrival)
    if mpf:
        raw = total_value * mpf["rate"] / 100
        amount = min(max(raw, mpf["min"]), mpf["max"])
        cap = "（按最低收费）" if raw < mpf["min"] else "（按最高收费）" if raw > mpf["max"] else ""
        layers.append(Layer("MPF 货物处理费", mpf["rate"], amount,
                            f"货值 ${total_value:,.2f} × {mpf['rate']}%，最低 ${mpf['min']}、最高 ${mpf['max']}{cap}",
                            mpf.get("note", "")))
        if total_value < policy.data.get("informal_limit_usd", 2500):
            warnings.append("整票货值低于 $2,500，可能按非正式报关处理，MPF 只收一笔小额固定费用。")
    if sea:
        hmf = policy.pick("hmf", arrival)
        if hmf:
            layers.append(Layer("HMF 港口维护费", hmf["rate"], total_value * hmf["rate"] / 100,
                                f"货值 ${total_value:,.2f} × {hmf['rate']}%", hmf.get("note", "")))
    return layers, warnings
