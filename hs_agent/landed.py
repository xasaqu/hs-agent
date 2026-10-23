"""美国 FBA 头程落地成本：一票货 + 一家报价 → 逐项费用、关税、退税净额和每件到仓成本（常态和偏坏两个数）。

只做自主清关和包税两种渠道的计算框架；亚马逊侧费用（入库配置费、卡派等）作为报价里的费用项手动填。
金额统一换算成人民币展示，关税和海关规费同时给出美元。
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .cn_tariff import CnTariff
from .duty import DutyResult, Layer, Policy, entry_fees, line_duty
from .tariff import fmt_code, normalize_code
from .us_hts import UsHts

LB50_KG = 22.68
LB100_KG = 45.36
SIDE_25IN_CM = 63.5

Driver = Literal["per_kg", "per_cbm", "per_carton", "per_shipment", "pct_value", "pct_freight", "per_day", "per_unit"]
DRIVER_NAMES = {"per_kg": "每公斤（计费重）", "per_cbm": "每方", "per_carton": "每箱", "per_shipment": "每票",
                "pct_value": "货值百分比", "pct_freight": "运费百分比", "per_day": "每天", "per_unit": "每件"}
# 燃油、旺季附加按运费的百分比收；报价写成「货值百分比」多半是识别错了
FREIGHT_PCT_CATS = ("燃油附加", "旺季附加")

DUTY_CAT = "关税和海关规费"
# 费用类别，以及识别用的关键词（按顺序匹配，先匹配到的为准）
CATEGORIES: list[tuple[str, list[str]]] = [
    ("关税垫付费", ["垫付", "垫税", "duty advance", "disbursement"]),
    ("燃油附加", ["燃油", "baf", "fuel", "lss"]),
    ("旺季附加", ["旺季", "pss", "peak", "gri", "涨价"]),
    ("ISF/AMS", ["isf", "ams", "10+2"]),
    ("报关费", ["报关", "export declaration", "出口申报", "文件费", "doc fee", "documentation"]),
    ("清关费", ["清关", "clearance", "customs entry", "broker"]),
    ("查验费", ["查验", "exam", "inspection"]),
    ("港口费", ["码头", "港口", "港杂", "thc", "ddc", "port", "车架", "chassis", "清洁", "换单", "卸货"]),
    ("仓储费", ["仓储", "堆存", "滞港", "滞箱", "demurrage", "detention", "storage", "暂存"]),
    ("提货费", ["提货", "上门", "pickup", "pick up"]),
    ("预约费", ["预约", "appointment"]),
    ("派送费", ["派送", "卡派", "送仓", "delivery", "trucking", "drayage", "尾程", "拖车", "ltl", "ftl", "偏远"]),
    ("操作费", ["操作", "贴标", "换标", "打托", "托盘", "拆柜", "分货", "handling", "入仓", "配置"]),
    ("保险", ["保险", "insurance"]),
    (DUTY_CAT, ["关税", "duty", "mpf", "hmf", "tariff", "税金", "税费"]),
    ("主干运费", ["运费", "海运", "空运", "头程", "干线", "freight", "ocean", "air", "海派", "空派", "单价"]),
]
CATEGORY_NAMES = [c for c, _ in CATEGORIES] + ["其他"]
REQUIRED = {  # 这类渠道一般都会产生的费用；报价里没写、也没注明已含的，提示「可能另收」
    ("sea", "self"): ["主干运费", "报关费", "清关费", "ISF/AMS", "派送费"],
    ("air", "self"): ["主干运费", "报关费", "清关费", "派送费"],
    ("sea", "ddp"): ["主干运费", "清关费", "派送费"],
    ("air", "ddp"): ["主干运费", "清关费", "派送费"],
}


# 「按实际另收」「到港后另计」这类只说会收、不说多少的写法：总成本里按 0 算，要追问范围和标准
VAGUE = re.compile(r"实报实销|按实际|按实报|实际产生|以实际|另计|另收|另行收取|到港后")


def guess_category(name: str) -> str:
    low = name.lower()
    for cat, words in CATEGORIES:
        if any(w in low for w in words):
            return cat
    return "其他"


class Product(BaseModel):
    name: str = ""
    cn_code: str = ""             # 中国 10 位编码，用来查退税率
    us_hts: str = ""              # 美国 8/10 位税号
    units: int = Field(ge=1)
    unit_value_usd: float = Field(ge=0)   # 申报单价（成交价）
    unit_cost_cny: float = 0      # 含税采购单价
    units_per_carton: int = Field(1, ge=1)
    carton_l: float = 0           # 外箱尺寸 cm
    carton_w: float = 0
    carton_h: float = 0
    carton_kg: float = 0          # 每箱毛重
    s232_rate: float = 0
    adcvd_rate: float = 0
    s301_rate: float | None = None
    exempt_12_5: bool | None = None

    @property
    def cartons(self) -> int:
        return math.ceil(self.units / self.units_per_carton)

    @property
    def cbm(self) -> float:
        return self.carton_l * self.carton_w * self.carton_h / 1e6 * self.cartons

    @property
    def kg(self) -> float:
        return self.carton_kg * self.cartons

    @property
    def value_usd(self) -> float:
        return self.unit_value_usd * self.units


class FeeItem(BaseModel):
    name: str
    category: str = ""
    driver: Driver = "per_shipment"
    rate: float = 0
    currency: Literal["CNY", "USD"] = "CNY"
    min_charge: float = 0
    rate_high: float | None = None   # 偏坏情况的单价（例如旺季、涨价后）
    qty: float | None = None         # 手动指定数量（例如滞港天数）
    scenario: bool = False           # 只计入偏坏情况（例如 USTR 港口费）
    carton_rule: str = ""            # 每箱计费时，只算满足条件的箱子，例如「重>22」「边>120 或 围>260」
    note: str = ""

    @model_validator(mode="after")
    def _fuel_on_freight(self):
        if self.driver == "pct_value" and (self.category or guess_category(self.name)) in FREIGHT_PCT_CATS:
            self.driver = "pct_freight"
        return self


class Quote(BaseModel):
    id: str = ""
    forwarder: str = ""
    channel: str = ""
    transport: Literal["sea", "air"] = "sea"
    clearance: Literal["self", "ddp"] = "self"   # 自主清关 / 包税
    transit_days: float = 0
    transit_days_high: float = 0
    divisor: float = 6000
    fenpao: float = 0          # 分泡：抛重部分让给客户的比例，0~1
    min_kg: float = 0
    min_kg_carton: float = 0   # 单箱最低计费重（例如不足 12kg 按 12kg）；填了就逐箱算计费重
    round_kg: float = 1
    includes: list[str] = []   # 主运费已经包含的费用类别
    items: list[FeeItem] = []
    note: str = ""

    @property
    def title(self) -> str:
        return " / ".join(x for x in (self.forwarder, self.channel) if x) or self.id or "报价"


class Risk(BaseModel):
    name: str
    prob: float = Field(0, ge=0, le=1)
    loss_cny: float = 0


class Settings(BaseModel):
    usd_cny: float = 7.1
    arrival: date | None = None         # 预计到港日；不填按今天 + 时效
    destination: str = ""               # 目的仓，例如「美西 ONT8」，报价提取时用来选档位
    can_refund: bool = True             # 有进项专票、用自己的抬头报关
    invoice_markup_pct: float = 0       # 工厂开票加点（占含税采购价）
    refund_wait_months: float = 3
    annual_rate_pct: float = 6          # 资金成本（年化）
    daily_profit_cny: float = 0         # 晚到一天少赚多少（机会成本，单独展示）
    risks: list[Risk] = []


class Shipment(BaseModel):
    products: list[Product]
    settings: Settings = Settings()


def cn_item(cn: CnTariff, code: str):
    """中国编码查退税率文库；文库里是 8 位的，10 位写成「…00」也能查到。"""
    code = normalize_code(code or "")
    if not code or not cn.loaded:
        return None
    return cn.get(code) or (cn.get(code[:8]) if len(code) == 10 and code.endswith("00") else None)


def _vat(cn: CnTariff, code: str) -> float:
    item = cn_item(cn, code)
    try:
        return float((item.vat_rate if item else "13").split("/")[0]) / 100
    except ValueError:
        return 0.13


def _charge(kg: float, vol: float, fenpao: float) -> float:
    return vol - (vol - kg) * fenpao if vol > kg else kg


def weights(s: Shipment, q: Quote) -> dict:
    kg = sum(p.kg for p in s.products)
    cbm = sum(p.cbm for p in s.products)
    vol = cbm * 1e6 / q.divisor if q.divisor else 0
    if q.min_kg_carton:  # 逐箱算：每箱的计费重不低于单箱最低计费重，再加总
        charge = 0.0
        for p in s.products:
            box_vol = p.carton_l * p.carton_w * p.carton_h / q.divisor if q.divisor else 0
            charge += max(_charge(p.carton_kg, box_vol, q.fenpao), q.min_kg_carton) * p.cartons
    else:
        charge = _charge(kg, vol, q.fenpao)
    charge = max(charge, q.min_kg)
    if q.round_kg > 0:
        charge = math.ceil(round(charge / q.round_kg, 6)) * q.round_kg
    return {"actual_kg": round(kg, 2), "cbm": round(cbm, 3), "volume_kg": round(vol, 2), "chargeable_kg": round(charge, 2),
            "cartons": sum(p.cartons for p in s.products), "units": sum(p.units for p in s.products),
            "value_usd": round(sum(p.value_usd for p in s.products), 2),
            "boxes": [(p.cartons, p.carton_kg, sorted((p.carton_l, p.carton_w, p.carton_h), reverse=True)) for p in s.products]}


RULE_VAR = {"重": "kg", "重量": "kg", "kg": "kg", "边": "side", "长": "side", "边长": "side", "最长边": "side", "side": "side",
            "围": "girth", "围长": "girth", "girth": "girth"}
RULE_TERM = re.compile(r"(最长边|边长|重量|围长|girth|side|kg|重|边|长|围)\s*(>=|<=|≥|≤|>|<)\s*(\d+(?:\.\d+)?)", re.I)


def parse_rule(rule: str) -> list[list[tuple[str, str, float]]]:
    """「边>120 或 围>260」→ 或的每一组里，「且」连起来的条件都要满足。围长 = 最长边 + 2 ×（另外两边之和）。"""
    groups = []
    for part in re.split(r"或|\||;|；", rule or ""):
        terms = [(RULE_VAR[m.group(1).lower()], m.group(2).replace("≥", ">=").replace("≤", "<="), float(m.group(3)))
                 for m in RULE_TERM.finditer(part)]
        if terms:
            groups.append(terms)
    return groups


def box_matches(groups: list, kg: float, sides: list[float]) -> bool:
    vals = {"kg": kg, "side": sides[0], "girth": sides[0] + 2 * (sides[1] + sides[2])}
    ops = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
    return any(all(ops[op](vals[v], n) for v, op, n in g) for g in groups)


def packing_checks(s: Shipment) -> list[dict]:
    """亚马逊入库的装箱硬约束。"""
    out = []
    for p in s.products:
        name = p.name or p.us_hts or "商品"
        sides = [x for x in (p.carton_l, p.carton_w, p.carton_h) if x]
        if not sides or not p.carton_kg:
            out.append({"level": "warn", "text": f"{name}：没填箱规或毛重，体积重和计费重会偏小。"})
            continue
        if p.units_per_carton > 1:
            if p.carton_kg > LB50_KG:
                out.append({"level": "error", "text": f"{name}：多件装的箱子毛重 {p.carton_kg:g}kg，超过亚马逊 50 磅（22.68kg）上限，要拆箱。"})
            if max(sides) > SIDE_25IN_CM:
                out.append({"level": "error", "text": f"{name}：箱子最长边 {max(sides):g}cm，超过 25 英寸（63.5cm）上限，要换箱。"})
        else:
            if p.carton_kg > LB100_KG:
                out.append({"level": "warn", "text": f"{name}：单件超过 100 磅，要贴 Mechanical Lift 标签。"})
            elif p.carton_kg > LB50_KG:
                out.append({"level": "warn", "text": f"{name}：单件超过 50 磅，要贴 Team Lift 标签。"})
    return out


def _qty(item: FeeItem, w: dict, days: float) -> tuple[float, str]:
    if item.qty is not None:
        return item.qty, "手动指定"
    d = item.driver
    if d == "per_kg":
        return w["chargeable_kg"], f"计费重 {w['chargeable_kg']:g}kg"
    if d == "per_cbm":
        return w["cbm"], f"{w['cbm']:g} 方"
    if d == "per_carton":
        groups = parse_rule(item.carton_rule)
        if groups:
            n = sum(c for c, kg, sides in w["boxes"] if box_matches(groups, kg, sides))
            return n, f"{n} 箱（{item.carton_rule}）"
        return w["cartons"], f"{w['cartons']} 箱"
    if d == "per_unit":
        return w["units"], f"{w['units']} 件"
    if d == "pct_value":
        return w["value_usd"] / 100, f"货值 ${w['value_usd']:,.2f} 的百分比"
    if d == "per_day":
        return days, f"{days:g} 天"
    return 1, "每票"


def calc(s: Shipment, q: Quote, hts: UsHts, policy: Policy, cn: CnTariff) -> dict:
    st = s.settings
    fx = st.usd_cny
    w = weights(s, q)
    arrival = st.arrival or date.today() + timedelta(days=int(q.transit_days or 30))
    checks = packing_checks(s)
    items: list[dict] = []

    def add(name, category, basis, normal_cny, high_cny, source, usd=None):
        items.append({"name": name, "category": category, "basis": basis, "cny": round(normal_cny, 2),
                      "cny_high": round(high_cny, 2), "usd": None if usd is None else round(usd, 2), "source": source})

    # 按运费百分比收的（燃油附加等）放到最后算，基数是前面各项里的主干运费
    for it in sorted(q.items, key=lambda x: x.driver == "pct_freight"):
        n, basis = _qty(it, w, it.qty or 0)
        cur = fx if it.currency == "USD" else 1
        unit = "美元" if it.currency == "USD" else "元"
        high_rate = it.rate_high if it.rate_high is not None else it.rate
        if it.driver == "pct_value":  # 货值是美元，按货值的百分比算出来先是美元
            normal_raw, high_raw = it.rate * n * fx, high_rate * n * fx
            txt = f"货值 ${w['value_usd']:,.2f} × {it.rate:g}%"
        elif it.driver == "pct_freight":
            base_n = sum(i["cny"] for i in items if i["category"] == "主干运费")
            base_h = sum(i["cny_high"] for i in items if i["category"] == "主干运费")
            normal_raw, high_raw = it.rate / 100 * base_n, high_rate / 100 * base_h
            txt = f"主干运费 {base_n:,.2f} 元 × {it.rate:g}%"
            if not base_n:
                checks.append({"level": "warn", "text": f"「{it.name}」按运费百分比计，但报价里没有主干运费，算出来是 0。"})
        else:
            normal_raw, high_raw = it.rate * n * cur, high_rate * n * cur
            txt = f"{it.rate:g} {unit} × {basis}"
        floor = it.min_charge * cur
        if it.min_charge:
            txt += f"，最低 {it.min_charge:g} {unit}"
        normal = 0 if it.scenario else max(normal_raw, floor)
        high = max(high_raw, floor)
        if it.scenario:
            txt += "（情景，只计入偏坏情况）"
        add(it.name, it.category or guess_category(it.name), txt, normal, high, "报价" + (f"：{it.note}" if it.note else ""))

    # 关税：自主清关由卖家交；包税渠道已含在运费单价里，只作参考
    duties: list[dict] = []
    duty_usd = 0.0
    for k, p in enumerate(s.products):
        if not p.us_hts:
            checks.append({"level": "warn", "text": f"{p.name or '商品'}：没填美国税号，关税按 0 计算。"})
            continue
        r: DutyResult = line_duty(hts, policy, p.us_hts, p.value_usd, p.units, p.kg, arrival,
                                  p.s232_rate, p.adcvd_rate, p.s301_rate, p.exempt_12_5)
        duty_usd += r.total
        duties.append(dict(r.public(), product=p.name, idx=k))
    fees, fee_warn = entry_fees(policy, w["value_usd"], q.transport == "sea", arrival)
    fee_usd = sum(l.amount for l in fees)
    if q.clearance == "self":
        for d in duties:
            add(f"关税：{d['product'] or d['code']}", DUTY_CAT,
                "；".join(f"{l['name']} {l['rate']:g}%" for l in d["layers"]) or "免税",
                d["total"] * fx, d["total"] * fx, "美国税则", d["total"])
        for l in fees:
            add(l.name, DUTY_CAT, l.basis, l.amount * fx, l.amount * fx, l.source or "美国海关规费", l.amount)
        checks += [{"level": "warn", "text": t} for t in fee_warn]
    implied = None
    if q.clearance == "ddp":
        tax_cny = (duty_usd + fee_usd) * fx
        freight = sum(i["cny"] for i in items if i["category"] == "主干运费")
        implied = {"duty_cny": round(tax_cny, 2), "per_kg": round(tax_cny / w["chargeable_kg"], 2) if w["chargeable_kg"] else 0,
                   "freight_per_kg": round(freight / w["chargeable_kg"], 2) if w["chargeable_kg"] else 0}

    # 风险的期望损失（概率 × 损失）
    for rk in st.risks:
        if rk.prob and rk.loss_cny:
            add(f"风险：{rk.name}", "风险", f"概率 {rk.prob:.0%} × 损失 {rk.loss_cny:,.0f} 元", rk.prob * rk.loss_cny, rk.prob * rk.loss_cny, "估算")

    goods_cny = sum(p.unit_cost_cny * p.units for p in s.products)
    # 资金占用：货款 + 头程 + 关税，在途期间的资金成本
    days_n, days_h = q.transit_days, q.transit_days_high or q.transit_days
    base_n = goods_cny + sum(i["cny"] for i in items)
    base_h = goods_cny + sum(i["cny_high"] for i in items)
    if st.annual_rate_pct and days_n:
        add("在途资金占用", "资金", f"（货款 + 头程 + 税费）× {st.annual_rate_pct:g}% 年化 × {days_n:g} 天",
            base_n * st.annual_rate_pct / 100 * days_n / 365, base_h * st.annual_rate_pct / 100 * days_h / 365, "估算")

    # 出口退税（只适用于自主报关、有进项专票）
    refunds = []
    for p in s.products:
        item = cn_item(cn, p.cn_code)
        rate = item.refund_rate if item and item.refund_rate is not None else None
        row = {"product": p.name, "cn_code": fmt_code(normalize_code(p.cn_code)) if p.cn_code else "", "rate": rate,
               "refund": 0.0, "markup": 0.0, "capital": 0.0, "net": 0.0, "note": ""}
        if q.clearance == "ddp":
            row["note"] = "包税渠道通常不是用自己的抬头报关，拿不到退税"
        elif not st.can_refund:
            row["note"] = "没有进项专票或买单报关，不退税（不算损失）"
        elif rate is None:
            row["note"] = ("没填中国编码" if not p.cn_code else "退税率文库里没有这个编码，请填 10 位中国编码" if not item
                           else "这个编码没有退税率")
        else:
            gross = p.unit_cost_cny * p.units
            row["refund"] = gross / (1 + _vat(cn, p.cn_code)) * rate / 100
            row["markup"] = gross * st.invoice_markup_pct / 100
            row["capital"] = row["refund"] * st.annual_rate_pct / 100 * st.refund_wait_months / 12
            row["net"] = row["refund"] - row["markup"] - row["capital"]
        refunds.append({k: round(v, 2) if isinstance(v, float) else v for k, v in row.items()})
    refund_net = sum(r["net"] for r in refunds)

    cost_n = sum(i["cny"] for i in items)
    cost_h = sum(i["cny_high"] for i in items)
    # 按每个商品占的计费重分摊运杂费；关税按各自税额
    per_product = []
    total_w = sum(max(p.kg, p.cbm * 1e6 / q.divisor if q.divisor else 0) for p in s.products) or 1
    duty_by = {d["idx"]: d["total"] * fx for d in duties} if q.clearance == "self" else {}
    duty_sum = sum(duty_by.values())
    for k, (p, rf) in enumerate(zip(s.products, refunds)):
        share = max(p.kg, p.cbm * 1e6 / q.divisor if q.divisor else 0) / total_w
        own_duty = duty_by.get(k, 0)
        logi_n = (cost_n - duty_sum) * share + own_duty
        logi_h = (cost_h - duty_sum) * share + own_duty
        per_product.append({
            "product": p.name or p.us_hts, "units": p.units,
            "logistics_per_unit": round(logi_n / p.units, 2), "logistics_per_unit_high": round(logi_h / p.units, 2),
            "landed_per_unit": round((logi_n + p.unit_cost_cny * p.units - rf["net"]) / p.units, 2),
            "landed_per_unit_high": round((logi_h + p.unit_cost_cny * p.units - rf["net"]) / p.units, 2),
            "refund_per_unit": round(rf["net"] / p.units, 2),
        })

    return {
        "quote": q.title, "quote_id": q.id, "transport": q.transport, "clearance": q.clearance,
        "arrival": arrival.isoformat(), "weights": w, "checks": checks, "items": items, "duties": duties,
        "implied_ddp": implied, "refunds": refunds,
        "totals": {"cost": round(cost_n, 2), "cost_high": round(cost_h, 2), "refund_net": round(refund_net, 2),
                   "goods": round(goods_cny, 2), "landed": round(cost_n + goods_cny - refund_net, 2),
                   "landed_high": round(cost_h + goods_cny - refund_net, 2),
                   "per_kg": round(cost_n / w["chargeable_kg"], 2) if w["chargeable_kg"] else 0},
        "per_product": per_product, "transit_days": q.transit_days, "transit_days_high": days_h,
        "includes": q.includes,
    }


def _by_cat(result: dict, high: bool = False) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in result["items"]:
        out[i["category"]] = out.get(i["category"], 0) + i["cny_high" if high else "cny"]
    return out


def compare(results: list[dict], quotes: list[Quote], daily_profit: float = 0) -> list[dict]:
    """多家报价放在一起：没写明的费用、别家都有这家没有的、单价明显偏离的、时效差带来的机会成本。"""
    cats = [_by_cat(r) for r in results]
    fastest = min((q.transit_days for q in quotes if q.transit_days), default=0)
    out = []
    for i, (r, q) in enumerate(zip(results, quotes)):
        flags = []
        listed = {c for c, v in cats[i].items() if v > 0} | set(q.includes)
        vague = [it.name for it in q.items if not it.rate and VAGUE.search(f"{it.name} {it.note}")]
        if vague:
            flags.append({"level": "warn", "text": f"「{'、'.join(vague)}」没给价格（按实际另收），总成本里按 0 算；"
                                                   "要追问收费标准和大概金额，最好写进报价。"})
        if VAGUE.search(q.note):
            flags.append({"level": "warn", "text": "报价备注里有「实报实销 / 另计」之类的写法，要让货代列明包括哪些费用、按什么标准收。"})
        for c in REQUIRED.get((q.transport, q.clearance), []):
            if c not in listed:
                flags.append({"level": "warn", "text": f"没写「{c}」，也没注明已含在运费里，可能另收。"})
        if len(results) >= 3:
            others = [j for j in range(len(results)) if j != i]
            for c in sorted({c for k in others for c in cats[k]}):
                have = [j for j in others if cats[j].get(c, 0) > 0]
                if c in listed or len(have) < 2 or c in ("风险", "资金", DUTY_CAT) or c in REQUIRED.get((q.transport, q.clearance), []):
                    continue
                flags.append({"level": "info", "text": f"其他 {len(have)} 家都列了「{c}」，这家没写。"})
            for c, v in cats[i].items():
                peers = [cats[j][c] for j in others if cats[j].get(c, 0) > 0]
                if len(peers) < 2 or v <= 0 or c in ("风险", "资金", DUTY_CAT):
                    continue
                med = statistics.median(peers)
                if v > med * 1.5:
                    flags.append({"level": "warn", "text": f"「{c}」{v:,.0f} 元，比其他家中位数 {med:,.0f} 元高 {v / med - 1:.0%}。"})
                elif v < med * 0.6:
                    flags.append({"level": "info", "text": f"「{c}」{v:,.0f} 元，比其他家中位数 {med:,.0f} 元低 {1 - v / med:.0%}，留意附加条件。"})
        if q.clearance == "ddp" and r.get("implied_ddp"):
            imp = r["implied_ddp"]
            flags.append({"level": "info", "text": f"包税单价每公斤 {imp['freight_per_kg']:g} 元，按税则测算其中关税约 {imp['per_kg']:g} 元/公斤；"
                                                   f"剩下的运费部分如果明显低于自主清关渠道，要警惕低报货值。"})
        opp = (q.transit_days - fastest) * daily_profit if daily_profit and q.transit_days and fastest else 0
        out.append({"quote": r["quote"], "quote_id": q.id, "cost": r["totals"]["cost"], "cost_high": r["totals"]["cost_high"],
                    "landed": r["totals"]["landed"], "landed_high": r["totals"]["landed_high"],
                    "refund_net": r["totals"]["refund_net"], "per_kg": r["totals"]["per_kg"],
                    "transit_days": q.transit_days, "opportunity": round(opp, 2), "flags": flags,
                    "errors": [c["text"] for c in r["checks"] if c["level"] == "error"]})
    return out


class BillItem(BaseModel):
    name: str
    amount: float
    currency: Literal["CNY", "USD"] = "CNY"
    category: str = ""


def check_bill(result: dict, bill: list[BillItem], usd_cny: float) -> dict:
    """实际账单和报价逐类对比：多收的、报价外的、没收的。"""
    quoted = _by_cat(result)
    billed: dict[str, float] = {}
    lines = []
    for b in bill:
        cat = b.category or guess_category(b.name)
        cny = b.amount * (usd_cny if b.currency == "USD" else 1)
        billed[cat] = billed.get(cat, 0) + cny
        lines.append({"name": b.name, "category": cat, "cny": round(cny, 2)})
    rows = []
    for c in [c for c in CATEGORY_NAMES + ["风险", "资金"] if c in quoted or c in billed]:
        if c in ("风险", "资金"):
            continue  # 估算项，账单上不会有
        qv, bv = quoted.get(c, 0), billed.get(c, 0)
        if bv and not qv:
            flag = "报价外费用"
        elif qv and not bv:
            flag = "报价有、账单没有"
        elif qv and bv > qv * 1.1:
            flag = f"超出报价 {bv / qv - 1:.0%}"
        elif qv and bv < qv * 0.9:
            flag = f"低于报价 {1 - bv / qv:.0%}"
        else:
            flag = ""
        rows.append({"category": c, "quoted": round(qv, 2), "billed": round(bv, 2), "diff": round(bv - qv, 2), "flag": flag})
    qt = sum(r["quoted"] for r in rows)
    bt = sum(r["billed"] for r in rows)
    return {"rows": rows, "lines": lines, "quoted_total": round(qt, 2), "billed_total": round(bt, 2),
            "diff": round(bt - qt, 2), "diff_pct": round((bt - qt) / qt, 4) if qt else None}
