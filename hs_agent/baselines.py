"""对照组：用同一个模型，不用 Agent，看逐层下钻到底带来多少提升。

- direct：直接问模型要 6 位编码（大多数人「用 AI 查 HS 编码」的方式）
- rag：先让模型给英文关键词，检索出候选子目，再让模型从候选里选一个（一次检索 + 一次选择，没有逐层核对）
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from openai import AsyncOpenAI

from .config import Config
from .tariff import Tariff, fmt_code, normalize_code

# 输入 messages，返回 (回复文本, 输入 token, 输出 token)
Completer = Callable[[list[dict]], Awaitable[tuple[str, int, int]]]


def make_completer(config: Config) -> Completer:
    client = AsyncOpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key)

    async def complete(messages: list[dict]) -> tuple[str, int, int]:
        r = await client.chat.completions.create(
            model=config.llm_model, messages=messages, temperature=0, response_format={"type": "json_object"},
        )
        u = r.usage
        return r.choices[0].message.content or "", u.prompt_tokens if u else 0, u.completion_tokens if u else 0

    return complete


@dataclass
class BaselineResult:
    hs6: str          # 模型给出的编码（规范化后），可能不存在于税则
    valid: bool       # 编码是否是税则里真实存在的 6 位子目
    confidence: float
    reason: str
    input_tokens: int
    output_tokens: int
    seconds: float
    cost_yuan: float
    error: str = ""


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def _cost(config: Config, i: int, o: int) -> float:
    return round((i * config.price_input_per_m + o * config.price_output_per_m) / 1_000_000, 4)


async def run_direct(description: str, tariff: Tariff, config: Config, complete: Completer) -> BaselineResult:
    started = time.time()
    messages = [
        {"role": "system", "content": "你是海关商品归类专家。按 HS 2022 版协调制度给出商品的 6 位编码。"
                                      '只输出 JSON：{"hs6": "6 位数字", "confidence": 0~1, "reason": "简要理由"}'},
        {"role": "user", "content": description},
    ]
    try:
        text, i, o = await complete(messages)
        data = _parse(text)
    except Exception as e:
        return BaselineResult("", False, 0, "", 0, 0, round(time.time() - started, 1), 0, f"{type(e).__name__}: {e}")
    code = normalize_code(str(data.get("hs6", "")))[:6]
    return BaselineResult(
        code, tariff.is_leaf(code), float(data.get("confidence", 0) or 0), str(data.get("reason", "")),
        i, o, round(time.time() - started, 1), _cost(config, i, o),
    )


async def run_rag(description: str, tariff: Tariff, config: Config, complete: Completer, k: int = 20) -> BaselineResult:
    started = time.time()
    ti = to = 0
    try:
        text, i, o = await complete([
            {"role": "system", "content": "把商品描述转成 HS 税则里的英文检索词（材质、功能、用途）。"
                                          '只输出 JSON：{"queries": ["关键词组 1", "关键词组 2", "关键词组 3"]}'},
            {"role": "user", "content": description},
        ])
        ti, to = ti + i, to + o
        queries = [str(q) for q in _parse(text).get("queries", [])][:5] or [description]
        scores: dict[str, float] = {}
        for q in queries:
            for code, s in tariff.search(q, k=k):
                scores[code] = max(scores.get(code, 0), s)
        candidates = sorted(scores, key=lambda c: -scores[c])[:k]
        lines = []
        for c in candidates:
            node = tariff.get(c)
            lines.append(f"{fmt_code(c)} {node.desc} ‖ 品目 {node.parent}: {tariff.get(node.parent).desc}")
        text, i, o = await complete([
            {"role": "system", "content": "你是海关商品归类专家。从候选的 HS 6 位子目里选出最符合商品的一个；都不合适也只能从候选里选最接近的。"
                                          '只输出 JSON：{"hs6": "6 位数字", "confidence": 0~1, "reason": "简要理由"}'},
            {"role": "user", "content": f"商品：{description}\n\n候选子目：\n" + "\n".join(lines)},
        ])
        ti, to = ti + i, to + o
        data = _parse(text)
    except Exception as e:
        return BaselineResult("", False, 0, "", ti, to, round(time.time() - started, 1), _cost(config, ti, to), f"{type(e).__name__}: {e}")
    code = normalize_code(str(data.get("hs6", "")))[:6]
    return BaselineResult(
        code, tariff.is_leaf(code), float(data.get("confidence", 0) or 0), str(data.get("reason", "")),
        ti, to, round(time.time() - started, 1), _cost(config, ti, to),
    )
