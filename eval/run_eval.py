"""评测运行器：同一批商品，分别用三种方式归类，比较准确率、校准度和成本。

  agent   逐层下钻 Agent（本项目）
  rag     检索候选 + 一次选择
  direct  直接问模型

用法：
  python -m eval.run_eval                                # 种子集，三种方式都跑
  python -m eval.run_eval --modes agent --limit 10       # 只跑 Agent 的前 10 条
  python -m eval.run_eval --cases eval/cross_cases.json  # 用官方裁定案例（先运行 eval.fetch_cross）
  python -m eval.run_eval --modes agent --repeat 3       # 每题跑 3 次，看平均准确率和波动
结果写到 agent_data/eval/<时间>/：results.csv 是明细，summary.md 是汇总。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path

from hs_agent.agent import classify, load_data
from hs_agent.baselines import make_completer, run_direct, run_rag
from hs_agent.config import Config
from hs_agent.tariff import fmt_code
from hs_agent.toolbox import no_more_info

SEED = Path(__file__).parent / "seed_cases.json"
MODES = ["agent", "rag", "direct"]
MODE_NAMES = {"agent": "逐层下钻 Agent", "rag": "检索 + 一次选择", "direct": "直接问模型"}


def match_digits(expected: str, got: str) -> dict:
    got = got or ""
    return {f"acc{n}": int(len(got) >= n and got[:n] == expected[:n]) for n in (2, 4, 6)}


async def run_one(mode: str, case: dict, config: Config, completer) -> dict:
    tariff, _ = load_data(config.hs_file, config.sections_file, config.cn_file)
    row = {"case_id": case["id"], "difficulty": case.get("difficulty", ""), "mode": mode, "expected": case["hs6"]}
    if mode == "agent":
        r = await classify(case["description"], config, answerer=no_more_info)
        a = r.answer
        got = a.hs6 if a else ""
        row.update({
            "got": got, "valid": int(bool(a)), "confidence": a.confidence if a else 0,
            "need_review": int(a.need_review) if a else 1, "questions": r.questions, "steps": r.steps,
            "rejections": r.finish_rejections, "seconds": r.seconds, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "cost_yuan": r.cost_yuan, "error": r.error, "log": str(r.log_path),
        })
        if case.get("cn10"):
            row["cn10_ok"] = int(bool(a) and a.cn_code == case["cn10"])
    else:
        fn = run_direct if mode == "direct" else run_rag
        r = await fn(case["description"], tariff, config, completer)
        row.update({
            "got": r.hs6, "valid": int(r.valid), "confidence": r.confidence,
            "need_review": int(r.confidence < config.review_threshold), "questions": 0,
            "steps": 1 if mode == "direct" else 2, "rejections": 0, "seconds": r.seconds,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cost_yuan": r.cost_yuan,
            "error": r.error, "log": "",
        })
    row.update(match_digits(case["hs6"], row["got"]))
    return row


def summarize(rows: list[dict], model: str, cases_file: str, threshold: float) -> str:
    modes = [m for m in MODES if any(r["mode"] == m for r in rows)]
    by = {m: [r for r in rows if r["mode"] == m] for m in modes}
    pct = lambda xs: f"{sum(xs) / len(xs):.0%}" if xs else "-"
    avg = lambda xs: sum(xs) / len(xs) if xs else 0

    def calib(rs: list[dict], high: bool) -> str:
        sel = [r for r in rs if (r["confidence"] >= threshold) == high]
        return f"{pct([r['acc6'] for r in sel])}（{len(sel)} 条）" if sel else "-（0 条）"

    n_cases = len({r["case_id"] for r in rows})
    lines = [
        f"# HS 归类评测（模型 {model}，{n_cases} 个商品，评测集 {cases_file}）",
        "",
        "| 指标 | " + " | ".join(MODE_NAMES[m] for m in modes) + " |",
        "| --- | " + " | ".join("---" for _ in modes) + " |",
    ]
    table = [
        ("章（2 位）准确率", lambda rs: pct([r["acc2"] for r in rs])),
        ("品目（4 位）准确率", lambda rs: pct([r["acc4"] for r in rs])),
        ("**子目（6 位）准确率**", lambda rs: f"**{pct([r['acc6'] for r in rs])}**"),
        ("编码不存在的比例", lambda rs: pct([1 - r["valid"] for r in rs])),
        (f"高置信（≥{threshold:g}）时的 6 位准确率", lambda rs: calib(rs, True)),
        (f"低置信（<{threshold:g}）时的 6 位准确率", lambda rs: calib(rs, False)),
        ("平均追问次数", lambda rs: f"{avg([r['questions'] for r in rs]):.2f}"),
        ("平均步数", lambda rs: f"{avg([r['steps'] for r in rs]):.1f}"),
        ("平均耗时（秒）", lambda rs: f"{avg([float(r['seconds']) for r in rs]):.1f}"),
        ("平均成本（元/件）", lambda rs: f"{avg([float(r['cost_yuan']) for r in rs]):.4f}"),
        ("出错次数", lambda rs: str(sum(1 for r in rs if r["error"]))),
    ]
    for name, fn in table:
        lines.append(f"| {name} | " + " | ".join(fn(by[m]) for m in modes) + " |")

    order = ["易", "中", "难"]
    difficulties = sorted({r["difficulty"] for r in rows if r["difficulty"]}, key=lambda d: (order.index(d) if d in order else 9, d))
    if len(difficulties) > 1:
        lines += ["", "按难度的 6 位准确率：", "",
                  "| 难度 | " + " | ".join(MODE_NAMES[m] for m in modes) + " |",
                  "| --- | " + " | ".join("---" for _ in modes) + " |"]
        for d in difficulties:
            lines.append(f"| {d} | " + " | ".join(pct([r["acc6"] for r in by[m] if r["difficulty"] == d]) for m in modes) + " |")

    cn = [r for r in by.get("agent", []) if "cn10_ok" in r]
    if cn:
        lines += ["", f"中国 10 位编码准确率（Agent）：{pct([r['cn10_ok'] for r in cn])}（{len(cn)} 条）"]

    wrong = [r for r in by.get("agent", []) if not r["acc6"]]
    if wrong:
        lines += ["", "Agent 判错的商品（逐条看日志，是复盘和迭代的主要材料）：", "",
                  "| 商品 | 标准答案 | Agent | 置信度 | 日志 |", "| --- | --- | --- | --- | --- |"]
        for r in wrong:
            got = fmt_code(r["got"]) if r["got"] else "未完成"
            lines.append(f"| {r['case_id']} | {fmt_code(r['expected'])} | {got} | {r['confidence']:.2f} | {Path(r['log']).name if r['log'] else ''} |")
    return "\n".join(lines) + "\n"


def summarize_repeats(rows: list[dict], repeat: int) -> str:
    """多次运行时：每轮的 6 位准确率、平均值和波动，以及每题答对几次。"""
    lines = ["", f"## 重复运行（每题 {repeat} 次）", ""]
    for mode in [m for m in MODES if any(r["mode"] == m for r in rows)]:
        rs = [r for r in rows if r["mode"] == mode]
        per_round = [sum(r["acc6"] for r in rs if r["round"] == k) / max(1, sum(1 for r in rs if r["round"] == k))
                     for k in range(1, repeat + 1)]
        mean = sum(per_round) / len(per_round)
        lines.append(f"- {MODE_NAMES[mode]}：每轮 6 位准确率 " + "、".join(f"{x:.0%}" for x in per_round)
                     + f"；平均 **{mean:.1%}**，最高最低差 {max(per_round) - min(per_round):.0%}")
        hits: dict[str, list[int]] = {}
        for r in rs:
            hits.setdefault(r["case_id"], []).append(r["acc6"])
        counts = [sum(v) for v in hits.values()]
        lines.append(f"  - 每次都对 {counts.count(repeat)} 题，有时对 {sum(1 for c in counts if 0 < c < repeat)} 题，"
                     f"每次都错 {counts.count(0)} 题")
        unstable = sorted((cid for cid, v in hits.items() if 0 < sum(v) < repeat))
        never = sorted(cid for cid, v in hits.items() if sum(v) == 0)
        if never:
            lines.append("  - 每次都错：" + "、".join(never))
        if unstable:
            lines.append("  - 时对时错：" + "、".join(f"{c}（{sum(hits[c])}/{repeat}）" for c in unstable))
    return "\n".join(lines) + "\n"


async def main_async(args) -> None:
    config = Config()
    config.history_enabled = False  # 评测不能沿用记录库里的结果，也不往里写
    if not config.llm_api_key:
        raise SystemExit("缺少大模型 API Key：请在 .env 里设置 LLM_API_KEY（或 DEEPSEEK_API_KEY）。")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    if args.limit:
        cases = cases[: args.limit]
    modes = [m for m in args.modes.split(",") if m in MODES]
    completer = make_completer(config)
    sem = asyncio.Semaphore(args.concurrency)
    done = 0

    repeat = max(1, args.repeat)
    total = len(cases) * len(modes) * repeat

    async def guarded(mode: str, case: dict, round_no: int) -> dict:
        nonlocal done
        async with sem:
            row = await run_one(mode, case, config, completer)
        row["round"] = round_no
        done += 1
        mark = "✓" if row["acc6"] else "✗"
        tag = f" 第{round_no}轮" if repeat > 1 else ""
        print(f"[{done}/{total}]{tag} {mark} {mode:6} {case['id']}: 期望 {fmt_code(row['expected'])}，"
              f"得到 {fmt_code(row['got']) if row['got'] else '无'} {row['error']}", flush=True)
        return row

    rows = await asyncio.gather(*(guarded(m, c, k) for k in range(1, repeat + 1) for c in cases for m in modes))

    out_dir = config.data_dir / "eval" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(rows, config.llm_model, Path(args.cases).name, config.review_threshold)
    if repeat > 1:
        summary = summary.replace("（模型", f"（{repeat} 轮合计，模型", 1) + summarize_repeats(rows, repeat)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"明细：{out_dir / 'results.csv'}")


def main() -> None:
    p = argparse.ArgumentParser(description="HS 归类评测")
    p.add_argument("--cases", default=str(SEED))
    p.add_argument("--modes", default="agent,rag,direct")
    p.add_argument("--only", default="", help="只跑指定商品，逗号分隔")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--repeat", type=int, default=1, help="每题跑几次（大模型每次结果有波动，多跑几次看平均）")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
