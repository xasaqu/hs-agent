"""网页界面：用假模型跑一次带追问的归类，经由 HTTP 接口提交、回答追问、拿到结果。"""

import json
import time
import urllib.request

import pytest

from hs_agent.config import Config
from hs_agent.web import App, describe_event, serve
from tests.test_agent_loop import FINISH, ScriptedModel

PORT = 8799


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_for(job_id, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        job = call(f"/api/jobs/{job_id}")
        if pred(job):
            return job
        time.sleep(0.1)
    raise AssertionError(f"超时：{job}")


@pytest.fixture
def server(tmp_path):
    cfg = Config()
    cfg.notes_online = False
    cfg.data_dir = tmp_path
    cfg.cn_file = tmp_path / "none.csv"
    script = [
        ("ask_human", {"question": "额定功率是多少？"}),
        ("browse", {"code": "8516"}),
        ("finish", FINISH),
    ]
    app = App(cfg, model=ScriptedModel(script))
    srv = serve(app, PORT)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app
    srv.shutdown()


def test_ask_and_result_over_http(server):
    assert call("/api/status")["hs_count"] > 5000
    page = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/").read().decode()
    assert "HS 归类助手" in page

    job_id = call("/api/classify", {"description": "空气炸锅", "allow_ask": True})["id"]
    job = wait_for(job_id, lambda j: j["status"] == "asking")
    assert job["question"] == "额定功率是多少？"
    assert call(f"/api/jobs/{job_id}/answer", {"answer": "1500W"})["ok"]

    job = wait_for(job_id, lambda j: j["status"] in ("done", "error"))
    assert job["status"] == "done", job
    a = job["result"]["answer"]
    assert a["hs6_fmt"] == "8516.60" and a["alternatives"][0]["code"] == "8516.79"
    assert [n["label"] for n in a["path_nodes"]] == ["第XVI类", "第85章", "8516", "8516.60"]
    texts = [e["text"] for e in job["events"]]
    assert texts[0] == "追问：额定功率是多少？" and job["events"][0]["detail"] == "回答：1500W"
    assert "比较品目 8516 下的全部子目" in texts and texts[-1].startswith("提交结果 8516.60")
    assert call("/api/jobs")[0]["hs6"] == "8516.60"


def test_empty_description_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/classify", {"description": "  "})
    assert e.value.code == 400


def test_missing_key_reports_error(tmp_path):
    cfg = Config()
    cfg.notes_online = False
    cfg.data_dir = tmp_path
    cfg.llm_api_key = ""
    app = App(cfg)
    job = app.start("空气炸锅", True)
    for _ in range(100):
        if job.status == "error":
            break
        time.sleep(0.05)
    assert job.status == "error" and "API Key" in job.error


def test_describe_event():
    assert describe_event({"event": "start"}) is None
    e = describe_event({"event": "action", "tool": "finish", "args": {"hs6": "851660"}, "ok": False, "note": "没看过品目"})
    assert e["kind"] == "warn" and e["detail"] == "没看过品目"


def test_already_running(server):
    from hs_agent.web import already_running
    assert already_running(PORT) and not already_running(PORT + 1)
