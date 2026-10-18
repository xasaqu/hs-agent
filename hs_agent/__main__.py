"""命令行入口：python -m hs_agent "家用电热空气炸锅，5L，塑料外壳，额定功率 1500W" """

from __future__ import annotations

import argparse
import asyncio
import json

from .agent import classify
from .config import Config
from .tariff import fmt_code
from .toolbox import no_more_info


def main() -> None:
    parser = argparse.ArgumentParser(description="HS 编码归类与出口退税查询 Agent")
    parser.add_argument("description", help="商品描述：名称、材质及占比、用途、工作原理等，越具体越准")
    parser.add_argument("--no-ask", action="store_true", help="不追问，信息不足时直接按最合理的假设归类")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    config = Config()
    kwargs = {"answerer": no_more_info} if args.no_ask else {}
    result = asyncio.run(classify(args.description, config, **kwargs))
    a = result.answer

    if args.json:
        print(json.dumps({"answer": a.model_dump() if a else None, "error": result.error}, ensure_ascii=False, indent=2))
        return

    print("\n========== 归类结果 ==========")
    if not a:
        print("没有完成归类。" + (f"原因：{result.error}" if result.error else ""))
    else:
        print(f"HS 编码：{fmt_code(a.hs6)}  {a.hs6_desc}")
        if a.cn_code:
            print(f"中国税则编码：{fmt_code(a.cn_code)}  {a.cn_name}")
            rate = "未收录" if a.refund_rate is None else f"{a.refund_rate:g}%"
            print(f"出口退税率：{rate}   增值税征税率：{a.vat_rate or '未注明'}   数据版本：{a.data_version or '未注明'}")
            if a.refund_note:
                print(f"注意：{a.refund_note}")
        else:
            print("中国 10 位编码、退税率：未导入中国税则数据，请到国家税务总局「出口退税率查询」核对。")
        print(f"置信度：{a.confidence:.0%}" + ("（建议人工复核或申请预裁定）" if a.need_review else ""))
        print(f"\n归类要素：{a.product_summary}")
        print("\n归类路径：")
        for s in a.steps:
            print(f"- {s.code}：{s.reason}")
        if a.rules:
            print(f"\n依据：{'、'.join(a.rules)}")
        if a.missing_info:
            print(f"缺少的信息：{'；'.join(a.missing_info)}")
        if a.alternatives:
            print(f"其他可能：{'、'.join(fmt_code(c) for c in a.alternatives)}")
        print("\n以上结果仅供参考，不构成归类依据，正式结论以海关审核或预裁定为准。")
    print(
        f"\n步数 {result.steps}，追问 {result.questions} 次，耗时 {result.seconds} 秒，"
        f"token {result.input_tokens}+{result.output_tokens}，约 {result.cost_yuan} 元。日志：{result.log_path}"
    )


if __name__ == "__main__":
    main()
