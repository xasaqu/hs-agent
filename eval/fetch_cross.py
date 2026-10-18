"""下载美国海关 CROSS 归类裁定评测集（ATLAS 论文公开的 200 条测试集），转换成本项目的评测格式。

数据：https://huggingface.co/datasets/flexifyai/cross_rulings_hts_dataset_for_tariffs
论文：ATLAS: Benchmarking and Adapting LLMs for Global Trade via Harmonized Tariff Code Classification（arXiv 2509.18400）
裁定给的是美国 10 位 HTS 编码，前 6 位就是国际通用的 HS 编码，所以可以用来评测 6 位准确率。
数据集没有注明许可证，只用于个人评测，不要再分发。

用法：python -m eval.fetch_cross            # 生成 eval/cross_cases.json
      python -m eval.fetch_cross --split validation
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from hs_agent.config import Config
from hs_agent.tariff import Tariff

DATASET = "flexifyai/cross_rulings_hts_dataset_for_tariffs"
API = "https://datasets-server.huggingface.co"
CODE_RE = re.compile(r"\b(\d{4})\.(\d{2})\.(\d{2})(?:\.?(\d{2}))?\b")


def get(path: str, **params) -> dict:
    url = f"{API}/{path}?" + urllib.parse.urlencode({"dataset": DATASET, **params})
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def split_roles(row: dict) -> tuple[str, str]:
    """返回 (用户输入, 模型输出)。兼容 messages 列表和 prompt/response 两种常见格式。"""
    for key in ("messages", "conversations", "conversation"):
        msgs = row.get(key)
        if isinstance(msgs, list):
            user = [m.get("content") or m.get("value") or "" for m in msgs if m.get("role", m.get("from")) in ("user", "human")]
            bot = [m.get("content") or m.get("value") or "" for m in msgs if m.get("role", m.get("from")) in ("assistant", "gpt")]
            return "\n".join(user), "\n".join(bot)
    user = next((row[k] for k in ("prompt", "input", "instruction", "question") if isinstance(row.get(k), str)), "")
    bot = next((row[k] for k in ("response", "output", "completion", "answer") if isinstance(row.get(k), str)), "")
    return user, bot


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", default="test")
    p.add_argument("--out", default=str(Path(__file__).parent / "cross_cases.json"))
    args = p.parse_args()

    splits = get("splits")["splits"]
    config_name = next(s["config"] for s in splits if s["split"] == args.split)
    rows, offset = [], 0
    while True:
        page = get("rows", config=config_name, split=args.split, offset=offset, length=100)
        batch = [r["row"] for r in page.get("rows", [])]
        rows += batch
        offset += len(batch)
        if not batch or offset >= page.get("num_rows_total", 0):
            break
    print(f"下载了 {len(rows)} 条（{args.split}）。字段：{list(rows[0].keys()) if rows else []}")

    cfg = Config()
    tariff = Tariff(cfg.hs_file, cfg.sections_file)
    cases, no_code, not_in_hs2022 = [], 0, 0
    for i, row in enumerate(rows):
        user, bot = split_roles(row)
        codes = CODE_RE.findall(bot)
        if not user or not codes:
            no_code += 1
            continue
        a, b, c, d = codes[-1]  # 推理过程里可能提到多个编码，最后一个是结论
        hs6 = a + b
        if not tariff.is_leaf(hs6):  # 早年裁定可能用的是旧版 HS，新版里已不存在
            not_in_hs2022 += 1
            continue
        cases.append({"id": f"cross-{args.split}-{i}", "description": user.strip(), "hs6": hs6,
                      "hts10": a + b + c + d, "difficulty": "官方裁定", "source": "CBP CROSS"})

    Path(args.out).write_text(json.dumps({"说明": f"来自 {DATASET} 的 {args.split} 集", "cases": cases},
                                         ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"转换成功 {len(cases)} 条；找不到编码 {no_code} 条；编码不在 HS 2022 里 {not_in_hs2022} 条。")
    print(f"已写入 {args.out}。抽查前 2 条：")
    for case in cases[:2]:
        print(f"- {case['hs6']} ← {case['description'][:150]}")


if __name__ == "__main__":
    main()
