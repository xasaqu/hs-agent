"""归类记录库：存档、相同商品复用、相似判例、人工确认和修正。"""

import asyncio
import json
import threading
import urllib.parse
import urllib.request

import pytest

from hs_agent.agent import classify, load_data
from hs_agent.config import Config
from hs_agent.history import History, HistoryError, answer_of, grams, normalize, precedents_text, similarity
from hs_agent.toolbox import Classification, Step
from hs_agent.web import App, serve
from tests.test_agent_loop import FINISH, ScriptedModel

PORT = 8796


def cfg(tmp_path) -> Config:
    c = Config()
    c.notes_online = False
    c.data_dir = tmp_path
    c.cn_file = tmp_path / "none.csv"
    return c


def ans(hs6="851660", conf=0.85) -> Classification:
    return Classification(hs6=hs6, hs6_desc="x", path="", product_summary="空气炸锅", steps=[Step(code="8516", reason="电热")],
                          rules=["规则一"], confidence=conf, need_review=conf < 0.7, missing_info=[], alternatives=[])


def tables(c):
    return load_data(c.hs_file, c.sections_file, c.cn_file)


def test_normalize_and_similarity():
    assert normalize("空气炸锅，5L （1500W）") == normalize("空气炸锅 5l 1500w")
    a, b = grams("家用电热空气炸锅，5L"), grams("空气炸锅 5L 塑料外壳")
    assert 0.4 < similarity(a, b) < 1 and similarity(a, grams("男式棉制T恤")) < 0.1


def test_exact_reuse_skips_model(tmp_path):
    c = cfg(tmp_path)
    script = [("browse", {"code": "8516"}), ("finish", FINISH)]
    r1 = asyncio.run(classify("空气炸锅，5L", c, model=ScriptedModel(script)))
    assert r1.record_id and not r1.reused
    # 第二次描述只差标点空格：不调用模型（空脚本的模型一调用就会出错）
    r2 = asyncio.run(classify("空气炸锅 5L", c, model=ScriptedModel([])))
    assert r2.reused == f"记录 #{r1.record_id}（Agent 结果）" and r2.answer.hs6 == "851660" and r2.cost_yuan == 0
    assert '"reuse"' in r2.log_path.read_text(encoding="utf-8")
    # fresh=True 时重新归类，并另存一条
    r3 = asyncio.run(classify("空气炸锅 5L", c, model=ScriptedModel(script), fresh=True))
    assert not r3.reused and r3.record_id != r1.record_id


def test_correction_and_precedents(tmp_path):
    c = cfg(tmp_path)
    tariff, cn = tables(c)
    h = History(tmp_path / "h.db")
    rid = h.add("三轮电动自卸车，载重 500 千克，矿场用", ans("870410"))
    assert h.similar("三轮电动自卸车，载重 800 千克") == []  # 没确认过的不当判例
    with pytest.raises(HistoryError):
        h.correct(rid, "8711.99", "", tariff, cn)  # 不是 6 位子目
    rec = h.correct(rid, "8711.60", "三轮、电动机驱动，按摩托车归类", tariff, cn)
    assert rec.status == "corrected" and rec.code == "871160"
    a = answer_of(rec, tariff, cn, 0.7)
    assert a.hs6 == "871160" and a.confidence == 1 and "人工修正（原为 8704.10）" in a.steps[0].reason and "870410" in a.alternatives
    found = h.similar("三轮电动自卸车，载重 800 千克")
    assert [r.id for r, _ in found] == [rid]
    text = precedents_text(found)
    assert "8711.60" in text and "按摩托车归类" in text
    # 完全相同的描述：修正过的优先
    h.add("三轮电动自卸车，载重 500 千克，矿场用", ans("870410"))
    assert h.find_exact("三轮电动自卸车 载重500千克 矿场用").id == rid
    assert h.counts() == {"total": 2, "confirmed": 0, "corrected": 1}
    assert [r.id for r in h.list(status="corrected")] == [rid] and len(h.list(q="8711")) == 1


def test_precedents_reach_the_model(tmp_path):
    c = cfg(tmp_path)
    tariff, cn = tables(c)
    first = asyncio.run(classify("家用空气炸锅，5L，1500W", c, model=ScriptedModel([("browse", {"code": "8516"}), ("finish", FINISH)])))
    from hs_agent.history import open_history
    open_history(tmp_path / "history.db").confirm(first.record_id)
    model = RecordingModel([("browse", {"code": "8516"}), ("finish", FINISH)])
    r = asyncio.run(classify("家用空气炸锅，3L，1200W", c, model=model))
    assert r.precedents == 1 and not r.reused
    first_input = json.dumps(model.inputs[0], ensure_ascii=False)
    assert "参考判例" in first_input and "8516.60" in first_input and "人工确认" in first_input


class RecordingModel(ScriptedModel):
    def __init__(self, script):
        super().__init__(script)
        self.inputs = []

    async def get_response(self, system_instructions, input, *a, **kw):
        self.inputs.append(input)
        return await super().get_response(system_instructions, input, *a, **kw)


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_review_over_http(tmp_path):
    c = cfg(tmp_path)
    app = App(c)
    rid = app.history_db.add("电子变频器，控制电动机转速", ans("850440"))
    srv = serve(app, PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call("/api/status")["history"]["total"] == 1
        with pytest.raises(urllib.error.HTTPError) as e:
            call(f"/api/history/{rid}/correct", {"code": "12", "note": ""})
        assert e.value.code == 400
        out = call(f"/api/history/{rid}/correct", {"code": "9032.89", "note": "第十六类注释一排除第九十章"})
        assert out["record"]["status"] == "corrected" and out["answer"]["hs6_fmt"] == "9032.89"
        recs = call("/api/history?q=" + urllib.parse.quote("变频器"))["records"]
        assert recs[0]["code"] == "9032.89" and recs[0]["agent_code"] == "8504.40"
        assert call(f"/api/history/{rid}/confirm", {})["record"]["status"] == "corrected"  # 修正过的再确认，仍算修正
    finally:
        srv.shutdown()
