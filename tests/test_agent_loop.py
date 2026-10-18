"""用脚本化的假模型跑完整条 Agent 链路：Agents SDK 编排 → 工具 → 校验 → 结果与日志。

剧本故意先「凭记忆」直接提交，验证校验会拒绝并让 Agent 继续，而不是直接结束。
"""

import asyncio
import json

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall

from hs_agent.agent import classify
from hs_agent.config import Config


class ScriptedModel(Model):
    def __init__(self, script):
        self.script = script
        self.i = 0
        self.seen: list[str] = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kw):
        if isinstance(input, list):
            outs = [str(x.get("output", "")) for x in input if isinstance(x, dict) and x.get("type") == "function_call_output"]
            if outs:
                self.seen.append(outs[-1])
        name, args = self.script[self.i]
        self.i += 1
        call = ResponseFunctionToolCall(
            type="function_call", id=f"fc_{self.i}", call_id=f"call_{self.i}", name=name,
            arguments=json.dumps(args, ensure_ascii=False), status="completed",
        )
        return ModelResponse(output=[call], usage=Usage(requests=1, input_tokens=1000, output_tokens=100, total_tokens=1100), response_id=None)

    def stream_response(self, *a, **kw):
        raise NotImplementedError


FINISH = {
    "hs6": "8516.60", "cn_code": "", "product_summary": "家用电热空气炸锅",
    "steps": [{"code": "85", "reason": "电气设备"}, {"code": "8516", "reason": "家用电热器具"},
              {"code": "8516.60", "reason": "烹饪用的烤炉、炉具"}],
    "rules": ["规则一", "规则六"], "confidence": 0.85, "missing_info": [], "alternatives": ["8516.79"],
}


def test_full_loop(tmp_path):
    script = [
        ("search", {"keywords": "air fryer electric domestic cooking", "chapter": ""}),
        ("finish", FINISH),                       # 没浏览过品目，必须被拒绝
        ("ask_human", {"question": "额定功率是多少？"}),
        ("browse", {"code": "8516"}),
        ("finish", FINISH),
    ]
    cfg = Config()
    cfg.notes_online = False
    cfg.data_dir = tmp_path
    cfg.cn_file = tmp_path / "none.csv"

    async def answerer(q):
        return "1500W"

    model = ScriptedModel(script)
    r = asyncio.run(classify("空气炸锅", cfg, answerer=answerer, model=model))

    assert r.error == ""
    assert model.i == len(script)
    assert "提交被拒绝" in model.seen[1] and "用户回答：1500W" in model.seen[2]
    a = r.answer
    assert a.hs6 == "851660" and a.alternatives == ["851679"] and a.refund_rate is None
    assert r.finish_rejections == 1 and r.questions == 1 and r.steps == 5
    assert r.input_tokens == 5000 and r.cost_yuan > 0
    events = [json.loads(l)["event"] for l in r.log_path.read_text(encoding="utf-8").splitlines()]
    assert events[0] == "start" and events[-1] == "end"
