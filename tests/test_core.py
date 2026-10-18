"""不调用大模型：税则树、检索、提交校验、中国税则查表、评测打分。"""

import asyncio

import pytest

from eval.run_eval import match_digits, summarize
from hs_agent.baselines import run_direct, run_rag
from hs_agent.cn_tariff import CnTariff
from hs_agent.config import Config
from hs_agent.runlog import RunLog
from hs_agent.tariff import Tariff, fmt_code, normalize_code, tokenize
from hs_agent.toolbox import RunState, Step, Toolbox

CFG = Config()
TARIFF = Tariff(CFG.hs_file, CFG.sections_file)


def make_tb(tmp_path, cn_rows: str = "") -> Toolbox:
    cfg = Config()
    cfg.notes_online = False
    cfg.data_dir = tmp_path
    cfg.ensure_dirs()
    cn_file = tmp_path / "cn.csv"
    if cn_rows:
        cn_file.write_text("code,name,refund_rate,conditions,version,source\n" + cn_rows, encoding="utf-8")
    state = RunState(config=cfg, tariff=TARIFF, cn=CnTariff(cn_file), log=RunLog(tmp_path / "runs", "t"))
    return Toolbox(state)


def test_tree_shape():
    assert len(TARIFF.sections) == 21
    assert sum(1 for n in TARIFF.nodes.values() if n.level == 6) > 5000
    path = [n.code for n in TARIFF.path("851660")]
    assert path == ["XVI", "85", "8516", "851660"]
    assert TARIFF.is_leaf("8516.60") and not TARIFF.is_leaf("8516") and not TARIFF.is_leaf("999999") and not TARIFF.is_leaf("851699")


def test_codes_and_tokens():
    assert normalize_code("8516.60.1000") == "8516601000"
    assert fmt_code("851660") == "8516.60" and fmt_code("8516601000") == "8516.60.1000"
    assert "lithium" in tokenize("lithium-ion") and "accumulator" in tokenize("Accumulators")


@pytest.mark.parametrize("query,code", [
    ("smartphone", "851713"),
    ("t-shirt cotton knitted", "610910"),
    ("vacuum flask", "961700"),
    ("lithium-ion accumulator", "850760"),
    ("LED lamp", "853952"),
])
def test_search_top3(query, code):
    assert code in [c for c, _ in TARIFF.search(query, k=3)]


def test_finish_requires_existing_code_and_browsed_heading(tmp_path):
    tb = make_tb(tmp_path)
    args = dict(cn_code="", product_summary="空气炸锅", steps=[Step(code="8516", reason="电热")],
                rules=["规则一"], confidence=0.9, missing_info=[], alternatives=["841960", "123456"])
    assert "不是税则里存在的" in tb.finish("851699", **args)
    assert "还没有浏览过品目 8516" in tb.finish("851660", **args)
    out = asyncio.run(tb.browse("8516"))
    assert "8516.60" in out and "8516.79" in out
    assert tb.finish("851660", **args) == "已提交。"
    a = tb.s.answer
    assert a.hs6 == "851660" and not a.need_review and a.alternatives == ["841960"]
    assert tb.s.finish_rejections == 2


def test_low_confidence_needs_review(tmp_path):
    tb = make_tb(tmp_path)
    asyncio.run(tb.browse("851660"))  # 浏览子目也算看过同级子目
    tb.finish("851660", "", "x", [], [], 0.4, ["功率未知"], [])
    assert tb.s.answer.need_review


def test_cn_tariff_lookup_and_validation(tmp_path):
    rows = "8516601000,测试编码甲,13,,测试版,单元测试\n8516609000,测试编码乙,9,A,测试版,单元测试\n"
    tb = make_tb(tmp_path, rows)
    out = asyncio.run(tb.browse("851660"))
    assert "8516.60.1000" in out and "8516.60.9000" in out
    msg = tb.finish("851660", "", "x", [], [], 0.9, [], [])
    assert "cn_code 必须是" in msg
    assert tb.finish("851660", "8516.60.9000", "x", [], [], 0.9, [], []) == "已提交。"
    a = tb.s.answer
    assert a.cn_code == "8516609000" and a.refund_rate == 9 and a.conditions == "A"


def test_browse_root_and_missing_notes(tmp_path):
    tb = make_tb(tmp_path)
    assert "第XVI类" in asyncio.run(tb.browse(""))
    out = asyncio.run(tb.browse("85"))
    assert "8516" in out and "没有这一类/章的注释" in out
    assert "税则里没有" in asyncio.run(tb.browse("9999"))


def test_baselines_with_fake_model():
    async def fake(messages):
        user = messages[-1]["content"]
        if "queries" in messages[0]["content"]:
            return '{"queries": ["air fryer domestic cooking"]}', 10, 5
        if "候选子目" in user:
            assert "8516.60" in user
            return '{"hs6": "8516.60", "confidence": 0.8, "reason": "电热烹饪"}', 100, 20
        return '{"hs6": "851699", "confidence": 0.9, "reason": "编的"}', 10, 5

    d = asyncio.run(run_direct("空气炸锅", TARIFF, CFG, fake))
    assert d.hs6 == "851699" and not d.valid
    r = asyncio.run(run_rag("空气炸锅", TARIFF, CFG, fake))
    assert r.hs6 == "851660" and r.valid and r.input_tokens == 110


def test_judge_and_summary():
    assert match_digits("851660", "851679") == {"acc2": 1, "acc4": 1, "acc6": 0}
    assert match_digits("851660", "") == {"acc2": 0, "acc4": 0, "acc6": 0}
    base = dict(difficulty="易", questions=0, steps=3, seconds=1, cost_yuan=0.01, error="", log="", valid=1)
    rows = [
        {**base, "case_id": "a", "mode": "agent", "expected": "851660", "got": "851660", "confidence": 0.9, **match_digits("851660", "851660")},
        {**base, "case_id": "b", "mode": "agent", "expected": "851713", "got": "851714", "confidence": 0.5, **match_digits("851713", "851714")},
        {**base, "case_id": "a", "mode": "direct", "expected": "851660", "got": "851699", "confidence": 0.9, "valid": 0, **match_digits("851660", "851699")},
    ]
    s = summarize(rows, "fake", "seed", 0.7)
    assert "**50%**" in s and "| 编码不存在的比例 | 0% | 100% |" in s and "| b | 8517.13 | 8517.14 |" in s


def test_cross_row_parsing():
    from eval.fetch_cross import CODE_RE, split_roles
    row = {"messages": [{"role": "user", "content": "Classify: stainless steel wok"},
                        {"role": "assistant", "content": "Reasoning ... HTS US Code -> 7323.93.0080"}]}
    user, bot = split_roles(row)
    assert user.startswith("Classify") and CODE_RE.findall(bot)[-1][:2] == ("7323", "93")
    assert split_roles({"prompt": "p", "response": "r 8516.60.40"}) == ("p", "r 8516.60.40")


def test_bundled_cn_tariff():
    """随项目附带的退税率表（由出口退税率文库导入）能覆盖 HS 2022 的每个子目。"""
    cn = CnTariff(CFG.cn_file)
    if not cn.loaded:
        pytest.skip("没有导入退税率表")
    items = cn.under("851660")
    assert "85166050" in {it.code for it in items}
    assert cn.get("85171300").refund_rate is not None
    leaves = [c for c, n in TARIFF.nodes.items() if n.level == 6]
    assert all(cn.under(c) for c in leaves)
