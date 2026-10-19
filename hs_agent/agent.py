"""Agent 定义：OpenAI Agents SDK 编排，DeepSeek 做决策，工具来自 Toolbox。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from agents import (
    Agent,
    FunctionToolResult,
    Model,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunContextWrapper,
    Runner,
    ToolsToFinalOutputResult,
    function_tool,
    set_tracing_disabled,
)
from openai import AsyncOpenAI

from .cn_tariff import CnTariff
from .config import Config
from .history import History, answer_of, open_history, precedents_text
from .official_notes import OfficialNotesService
from .rules import GRI
from .runlog import RunLog
from .tariff import Tariff
from .toolbox import Answerer, Classification, RunState, Step, Toolbox, console_answerer

set_tracing_disabled(True)

INSTRUCTIONS = """你是一名资深的海关商品归类专家，按 HS 协调制度为出口商品确定编码。税则条文是英文的。

工作方法（逐层下钻，每一层都要有依据）：
1. 提取归类要素：商品是什么、材质及各材质占比、主要功能和用途、工作原理（是否电动/电热）、加工程度、是否零售成套、包装。
   缺少会改变归类结果的关键要素时，用 ask_human 问一次，把要问的点一次问全；不影响结果的信息不要问。
2. 用 search 检索候选：把商品转成税则里的英文说法（材质、功能、用途），可以多换几组关键词。检索结果只是线索。
3. 用 browse 逐层核对：打开候选所在的章，看类注、章注里有没有排除条款；再打开品目，读品目注释（本品目包括/不包括什么），比较它下面所有同级子目，选出最符合的一个。
   注释和品目条文一样有法律效力，和你的常识冲突时以注释为准，并在 steps 里引用。
   多个品目都说得通时，按归类总规则判断，并在 steps 里写清楚为什么排除另一个。
   候选分属不同的类或章时，两边的类注、章注都要读，并且双向核对排除条款：
   例如第十六类注释一、第三十九章注释二都把第九十章的物品排除在外，符合第九十章品目（如 90.01 光学元件、90.32 自动调节或控制仪器）的商品就不能因为「本类列名更具体」留在第八十四、八十五或三十九章。
   排除条款属于规则一，先于规则三(一)「列名具体优先」；只有规则一定不下来时，才用规则三。
4. 如果 browse 子目时看到了中国 8/10 位编码，继续在里面选出最符合的一个作为 cn_code；没有就留空。
5. 用 finish 提交：
   - steps 按「章 → 品目 → 子目（→ 10 位）」列出每一层选择的编码和理由；
   - rules 写用到的总规则（如「规则一」「规则三(一)」）和注释；
   - confidence 是你对 6 位子目正确的把握（0~1）。信息不足、要靠假设、或者在两个品目之间犹豫时，要明显低于 0.7；
   - missing_info 写还缺什么信息会影响结果；alternatives 写其他可能的 6 位子目。

规则：
- 只能提交税则里真实存在、你用 browse 核对过的编码。不要凭记忆写编码。
- 标题（类名、章名）只为查找方便，具有法律效力的是品目条文和注释（规则一）。
- 第二十八、二十九章原则上只包括单独的已有化学定义的化合物，以及章注一列出的形式（如水溶液）。为了某种用途有意加入其他成分（糖、盐、油脂、辅料、药用成分等）的混合物或配制品，一般要离开这两章，去看第二十一章、第三十章、第三十八章等配制品的品目。
- 退税率和监管条件由系统查表给出，你不需要也不要自己写。
- 每次只调用一个工具。

""" + GRI


def _tb(ctx: RunContextWrapper[RunState]) -> Toolbox:
    return Toolbox(ctx.context)


@function_tool
def search(ctx: RunContextWrapper[RunState], keywords: str, chapter: str) -> str:
    """用英文关键词检索 6 位子目候选。chapter 可填 2 位章号限定范围，不限定就传空字符串。"""
    return _tb(ctx).search(keywords, chapter)


@function_tool
async def browse(ctx: RunContextWrapper[RunState], code: str) -> str:
    """查看税则树的某个节点：空字符串=全部类；罗马数字=某类；2 位=章（含注释）；4 位=品目及其全部子目；6 位=子目及同级子目。"""
    return await _tb(ctx).browse(code)


@function_tool
def cn_codes(ctx: RunContextWrapper[RunState], hs6: str) -> str:
    """列出某个 6 位子目下的中国 8/10 位税则编码（需要导入中国税则数据）。"""
    return _tb(ctx).cn_codes(hs6)


@function_tool
async def ask_human(ctx: RunContextWrapper[RunState], question: str) -> str:
    """向用户追问会影响归类的关键信息，例如材质占比、用途、是否电动。"""
    return await _tb(ctx).ask_human(question)


@function_tool
def finish(
    ctx: RunContextWrapper[RunState],
    hs6: str,
    cn_code: str,
    product_summary: str,
    steps: list[Step],
    rules: list[str],
    confidence: float,
    missing_info: list[str],
    alternatives: list[str],
) -> str:
    """提交归类结果。hs6 是 6 位子目；cn_code 是中国 10 位编码，没有就传空字符串；product_summary 用中文概括归类要素。"""
    return _tb(ctx).finish(hs6, cn_code, product_summary, steps, rules, confidence, missing_info, alternatives)


TOOLS = [search, browse, cn_codes, ask_human, finish]


def stop_when_answered(ctx: RunContextWrapper[RunState], results: list[FunctionToolResult]) -> ToolsToFinalOutputResult:
    """finish 通过校验才结束；被拒绝时把原因还给模型继续改。"""
    if ctx.context.answer is not None:
        return ToolsToFinalOutputResult(is_final_output=True, final_output=ctx.context.answer)
    return ToolsToFinalOutputResult(is_final_output=False)


def make_model(config: Config) -> Model:
    client = AsyncOpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key)
    return OpenAIChatCompletionsModel(model=config.llm_model, openai_client=client)


def build_agent(config: Config, model: Model | None = None) -> Agent[RunState]:
    return Agent[RunState](
        name="HS 归类助手",
        instructions=INSTRUCTIONS,
        model=model or make_model(config),
        model_settings=ModelSettings(temperature=0),
        tools=TOOLS,
        tool_use_behavior=stop_when_answered,
    )


@lru_cache(maxsize=4)
def load_data(hs_file: Path, sections_file: Path, cn_file: Path) -> tuple[Tariff, CnTariff]:
    return Tariff(hs_file, sections_file), CnTariff(cn_file)


@dataclass
class RunResult:
    run_id: str
    log_path: Path
    answer: Classification | None
    steps: int
    questions: int
    finish_rejections: int
    seconds: float
    input_tokens: int
    output_tokens: int
    cost_yuan: float
    error: str = ""
    record_id: int = 0         # 存进记录库的编号（没存为 0）
    reused: str = ""           # 沿用了以前的记录时，写明来源，例如「记录 #12（人工修正）」
    precedents: int = 0        # 给 Agent 参考的判例条数


async def classify(
    description: str,
    config: Config | None = None,
    answerer: Answerer = console_answerer,
    model: Model | None = None,
    on_event: Callable[[dict], None] | None = None,
    fresh: bool = False,
) -> RunResult:
    """model 只在测试时传入（脚本化的假模型），正常运行用配置里的大模型。on_event 接收每一条运行日志。
    fresh=True 时不沿用记录库里相同商品的结果，重新归类（判例照样参考）。"""
    config = config or Config()
    config.ensure_dirs()
    tariff, cn = load_data(config.hs_file, config.sections_file, config.cn_file)
    history = open_history(config.data_dir / "history.db") if config.history_enabled else None
    log = RunLog(config.data_dir / "runs", description, listener=on_event)

    if history and not fresh:
        rec = history.find_exact(description)
        if rec:
            return _reuse(rec, history, tariff, cn, config, log)
    if model is None and not config.llm_api_key:
        raise SystemExit("缺少大模型 API Key：请在 .env 里设置 LLM_API_KEY（或 DEEPSEEK_API_KEY）。")

    prompt = f"请为下面的出口商品归类：\n{description}"
    found = history.similar(description) if history else []
    if found:
        prompt += "\n" + precedents_text(found)
        log.write("precedents", {"records": [{"id": r.id, "code": r.code, "status": r.status, "score": round(s, 2)} for r, s in found]})
    notes = OfficialNotesService(config)  # 不在线查询时也会读手动导入的注释
    state = RunState(config=config, tariff=tariff, cn=cn, log=log, answerer=answerer, notes=notes)
    agent = build_agent(config, model)

    error = ""
    input_tokens = output_tokens = 0
    try:
        result = await Runner.run(agent, prompt, context=state, max_turns=config.max_steps)
        usage = result.context_wrapper.usage
        input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
    except Exception as e:  # 超过步数上限、模型报错等，都如实记录
        error = f"{type(e).__name__}: {e}"
        log.write("error", {"error": error})
    finally:
        if notes:
            await notes.close()

    cost = (input_tokens * config.price_input_per_m + output_tokens * config.price_output_per_m) / 1_000_000
    log.write("end", {
        "answer": state.answer.model_dump() if state.answer else None,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_yuan": round(cost, 4),
    })
    record_id = history.add(description, state.answer, log.run_id) if history and state.answer else 0
    return RunResult(
        run_id=log.run_id, log_path=log.path, answer=state.answer, steps=log.steps, questions=log.questions,
        finish_rejections=state.finish_rejections, seconds=log.elapsed, input_tokens=input_tokens,
        output_tokens=output_tokens, cost_yuan=round(cost, 4), error=error, record_id=record_id, precedents=len(found),
    )


def _reuse(rec, history: History, tariff: Tariff, cn: CnTariff, config: Config, log: RunLog) -> RunResult:
    """描述相同的商品：直接沿用记录库里的结果，不调用大模型。"""
    answer = answer_of(rec, tariff, cn, config.review_threshold)
    history.touch(rec.id)
    source = f"记录 #{rec.id}（{rec.status_name}）"
    log.write("reuse", {"record_id": rec.id, "status": rec.status, "code": rec.code})
    log.write("end", {"answer": answer.model_dump(), "input_tokens": 0, "output_tokens": 0, "cost_yuan": 0, "reused": source})
    return RunResult(run_id=log.run_id, log_path=log.path, answer=answer, steps=0, questions=0, finish_rejections=0,
                     seconds=log.elapsed, input_tokens=0, output_tokens=0, cost_yuan=0.0, record_id=rec.id, reused=source)
