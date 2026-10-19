"""从海关总署「归类决定和归类裁定查询」整理中文评测集。

和官方注释查询一样用浏览器操作页面：选数据来源和「生效中」→ 查询 → 逐条点「详细」读出完整内容。
两次点「详细」之间按 NOTES_INTERVAL_S 间隔（默认 5 秒），已经抓过的编号会跳过，可以分几次跑完。

  python -m eval.fetch_rulings --max 120              # 抓取，原始内容存到 eval/rulings_raw.jsonl
  python -m eval.fetch_rulings --build-only           # 只根据已抓的原始内容重新生成评测集
  python -m eval.run_eval --cases eval/cn_cases.json  # 用官方案例评测

评测集 eval/cn_cases.json：描述只取商品本身的信息（名称、规格、商品描述），
不含归类理由和决定税号，避免把答案泄露给模型。只保留 2022 年及以后（HS 2022 版）的决定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from hs_agent.config import Config
from hs_agent.official_notes import BlankPage, NotesError, NotesFetcher

URL = "http://www.customs.gov.cn/customs/302427/302442/gljdhcd/index.html"
HERE = Path(__file__).parent
RAW = HERE / "rulings_raw.jsonl"
CASES = HERE / "cn_cases.json"
ID_RE = re.compile(r"^[A-Z]\d{4}-\d+$")

# 详细页面里可能出现的字段名；商品描述到下一个字段名为止
FIELDS = ["相关编号", "编号", "中文名称", "商品名称（中文）", "商品名称（英文）", "商品名称（其他）", "商品名称",
          "英文名称", "其他名称", "规格型号", "商品描述", "描述", "决定税号", "税则号列", "商品编码", "归类决定",
          "归类意见", "归类理由", "理由", "决定依据", "生效时间", "生效日期", "发布单位", "发布日期", "失效时间",
          "失效日期", "失效原因", "数据状态", "备注", "关闭", "关 闭"]
LABEL_RE = re.compile(r"(?m)^\s*(" + "|".join(map(re.escape, FIELDS)) + r")\s*(?:[:：]\s*|$)")


def load_raw() -> dict[str, dict]:
    if not RAW.exists():
        return {}
    out = {}
    for line in RAW.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["id"]] = r
    return out


class RulingsFetcher(NotesFetcher):
    async def run(self, sources: list[str], max_new: int, min_year: int) -> int:
        done = load_raw()
        added = 0
        context = await self._ensure_context()
        page = await context.new_page()
        page.set_default_timeout(self.config.notes_timeout_ms)
        try:
            for source in sources:
                added += await self._source(context, page, source, done, max_new - added, min_year)
                if added >= max_new:
                    break
        except (NotesError, BlankPage) as e:
            await self._dump(context, "rulings")
            why = str(e) or "页面一直是空白，网站校验没有通过"
            print(f"中断：{why}（这次已保存 {added} 条，再运行一次会从断点继续；调试文件在 agent_data/notes_debug/）")
        finally:
            await self.close()
        return added

    async def _query_frame(self, page):
        async def finder(frame):
            sel = frame.locator("select")
            return sel if await sel.count() >= 1 and await frame.locator("text=数据来源").count() else None
        frame, _ = await self._find_in_frames(page, finder, "「数据来源」下拉框")
        return frame

    async def _select(self, frame, label: str, option: str) -> None:
        """选下拉框里的 option。页面用的是 Kendo 下拉控件，先按 Kendo 的方式选；不行再试普通 select。"""
        ok = await page_eval(frame, KENDO_SELECT, option)
        if ok:
            return
        sels = frame.locator("select")
        for i in range(await sels.count()):
            sel = sels.nth(i)
            texts = await sel.locator("option").all_inner_texts()
            match = [t for t in texts if t.strip() == option]
            if match:
                await sel.select_option(label=match[0])
                return
        print(f"  （没能在「{label}」里选「{option}」，按页面默认值查询）")

    async def _source(self, context, page, source: str, done: dict, quota: int, min_year: int) -> int:
        if quota <= 0:
            return 0
        try:
            await self._open(page, URL)
        except BlankPage:  # 网站偶尔拦一次，等一会儿再试
            print("  页面空白，30 秒后重试…")
            await asyncio.sleep(30)
            await self._open(page, URL)
        frame = await self._query_frame(page)
        if source != "全部":  # 「全部」时不选数据来源，归类决定和归类裁定一次查出来，少打开一次页面
            await self._select(frame, "数据来源", source)
        await self._select(frame, "数据状态", "生效中")
        button = frame.get_by_role("button", name=re.compile(r"^\s*查\s*询\s*$"))
        if not await button.count():
            button = frame.locator("input[value='查询'], a:has-text('查询'), span:has-text('查询')")
        await button.first.click()
        await page.wait_for_timeout(2000)

        added, page_no, old_pages = 0, 1, 0
        while added < quota and old_pages < 10:  # 连续 10 页都是早年的决定就不再往后翻
            rows = await self._rows(frame)
            print(f"{source} 第 {page_no} 页：{len(rows)} 条")
            if not rows:
                break
            old_pages = old_pages + 1 if all(int(c[0][1:5]) < min_year for c, _ in rows) else 0
            for cells, row in rows:
                rid = cells[0]
                if int(rid[1:5]) < min_year or rid in done:
                    continue
                text = await self._detail(context, page, frame, row)
                rec = {"id": rid, "source": source if source != "全部" else "归类决定/裁定", "name": cells[1], "code": re.sub(r"\D", "", cells[2]),
                       "spec": cells[3], "detail": text, "fetched_at": time.time()}
                with RAW.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                done[rid] = rec
                added += 1
                print(f"  ✓ {rid} {cells[1]} → {rec['code']}（详细 {len(text)} 字）")
                if added >= quota:
                    break
                await asyncio.sleep(self.config.notes_interval_s)
            if added >= quota or not await self._next_page(frame, page_no, rows[0][0][0]):
                break
            page_no += 1
            await page.wait_for_timeout(2000)
        return added

    async def _rows(self, frame) -> list[tuple[list[str], object]]:
        out = []
        trs = frame.locator("tr")
        for i in range(await trs.count()):
            tr = trs.nth(i)
            tds = tr.locator("td")
            if await tds.count() < 4:
                continue
            cells = [(await tds.nth(j).inner_text()).strip() for j in range(min(await tds.count(), 5))]
            if ID_RE.match(cells[0]):
                out.append((cells, tr))
        return out

    async def _detail(self, context, page, frame, row) -> str:
        """点这一行的「详细」，读出新窗口或弹层里的文字，读完关掉。"""
        before = {id(p) for p in context.pages}
        texts_before = {}
        for f in page.frames:
            try:
                texts_before[f] = await f.locator("body").inner_text(timeout=2000)
            except Exception:
                pass
        btn = row.locator("button, a, input[type=button], span").filter(has_text=re.compile("详细|详情"))
        if not await btn.count():
            btn = row.locator("input[value='详细'], input[value='详情']")
        await btn.first.click()
        deadline = time.time() + self.config.notes_timeout_ms / 1000
        while time.time() < deadline:
            await page.wait_for_timeout(800)
            new_pages = [p for p in context.pages if id(p) not in before]
            for p in new_pages:
                try:
                    await p.wait_for_load_state("load", timeout=5000)
                    text = await p.locator("body").inner_text(timeout=3000)
                except Exception:
                    continue
                if len(text.strip()) > 20:
                    await p.close()
                    return text.strip()
            for f in page.frames:  # 页内弹层或新出现的 iframe：取多出来的文字
                try:
                    text = await f.locator("body").inner_text(timeout=2000)
                except Exception:
                    continue
                old = texts_before.get(f, "")
                if text != old and len(text) > len(old) + 20:
                    extra = text.replace(old, "").strip() if old and old in text else text.strip()
                    await self._close_modal(page)
                    return extra
        raise NotesError("点开「详细」后没有读到内容")

    @staticmethod
    async def _close_modal(page) -> None:
        for f in page.frames:
            btn = f.locator("button, a, input[type=button], span").filter(has_text=re.compile(r"^\s*关\s*闭\s*$|^\s*×\s*$"))
            try:
                if await btn.count():
                    await btn.first.click(timeout=2000)
                    return
            except Exception:
                pass
        await page.keyboard.press("Escape")

    async def _next_page(self, frame, page_no: int, first_id: str) -> bool:
        """翻到下一页。页面是 Kendo 表格：优先调用表格自己的翻页；不行再点分页条上的「下一页」箭头。
        翻成功的标志是第一行编号变了。"""
        body = await frame.locator("body").inner_text()
        m = re.search(r"共\s*(\d+)\s*页", body)
        if m and page_no >= int(m.group(1)):
            return False
        moved = await page_eval(frame, KENDO_PAGE, page_no + 1)
        if not moved:
            nxt = frame.locator("a[title*='下一页'], a[aria-label*='下一页'], a[title*='next' i], a[aria-label*='next' i], "
                                ".k-pager-nav:has(.k-i-arrow-e), .k-pager-nav:has(.k-i-arrow-60-right)")
            nxt = nxt.locator("visible=true")
            if not await nxt.count():
                print(f"  没找到翻页按钮，列表结束。页面末尾的文字：{' '.join(body.split()[-30:])}")
                return False
            await nxt.first.click()
        deadline = time.time() + self.config.notes_timeout_ms / 1000
        while time.time() < deadline:
            await frame.page.wait_for_timeout(700)
            rows = await self._rows(frame)
            if rows and rows[0][0][0] != first_id:
                return True
        print(f"  翻到第 {page_no + 1} 页后列表没有变化，列表结束。")
        return False


async def page_eval(frame, script: str, arg):
    """在页面自己的 JS 环境里执行。patchright 默认在隔离环境里执行，看不到页面的 jQuery，要显式关掉。"""
    try:
        return await frame.evaluate(script, arg, isolated_context=False)
    except TypeError:  # 普通 Playwright 没有这个参数，本来就在页面环境里执行
        return await frame.evaluate(script, arg)


# 在页面里执行的脚本：用 Kendo 控件自己的接口选下拉项、翻页（只是调用页面已有的功能，等同于手动点选）
KENDO_SELECT = """(option) => {
  const jq = window.jQuery || window.$;
  if (!jq) return false;
  for (const el of document.querySelectorAll('[data-role=dropdownlist]')) {
    const w = jq(el).data('kendoDropDownList');
    if (!w) continue;
    const items = w.dataSource.data();
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const text = (typeof it === 'string') ? it : (it[w.options.dataTextField] ?? it.text ?? '');
      if (String(text).trim() === option) { w.select(i + (w.options.optionLabel ? 1 : 0)); w.trigger('change'); return true; }
    }
  }
  return false;
}"""
KENDO_PAGE = """(n) => {
  const jq = window.jQuery || window.$;
  if (!jq) return false;
  for (const el of document.querySelectorAll('[data-role=grid], [data-role=listview], [data-role=pager]')) {
    const w = jq(el).data('kendoGrid') || jq(el).data('kendoListView') || jq(el).data('kendoPager');
    const ds = w && w.dataSource;
    if (ds && ds.totalPages && n <= ds.totalPages()) { ds.page(n); return true; }
  }
  return false;
}"""


def description_from(rec: dict) -> str:
    """评测用的商品描述：名称、规格，加上详细页里的商品描述段落。不含税号和归类理由。"""
    parts = [rec["name"]]
    if rec.get("spec") and not re.fullmatch(r"(无|无规格|-)\d*", rec["spec"]):
        parts.append(f"规格型号：{rec['spec']}")
    detail = rec.get("detail", "")
    matches = list(LABEL_RE.finditer(detail))
    for i, m in enumerate(matches):
        if m.group(1) in ("商品描述", "描述"):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(detail)
            body = detail[m.end():end].strip()
            if body:
                parts.append(body)
            break
    text = "。".join(p.strip("。 ") for p in parts if p)
    code = rec["code"]
    if code and (code in text or f"{code[:4]}.{code[4:6]}" in text):  # 描述里提到了答案，不能用
        return ""
    return text


def build(min_year: int) -> None:
    from hs_agent.agent import load_data
    cfg = Config()
    tariff, cn = load_data(cfg.hs_file, cfg.sections_file, cfg.cn_file)
    cases, skipped = [], []
    for rid, rec in sorted(load_raw().items()):
        code = rec.get("code", "")
        if int(rid[1:5]) < min_year or len(code) < 6:
            skipped.append((rid, "年份太早或没有税号"))
            continue
        hs6 = code[:6]
        node = tariff.get(hs6)
        if not node or not tariff.is_leaf(hs6):
            skipped.append((rid, f"{hs6} 不是 HS 2022 的子目"))
            continue
        desc = description_from(rec)
        if not desc:
            skipped.append((rid, "描述里含有答案"))
            continue
        case = {"id": rid, "description": desc, "hs6": hs6, "difficulty": "官方", "source": f"海关{rec['source']} {rid}"}
        if cn.get(code):
            case["cn10"] = code
        cases.append(case)
    CASES.write_text(json.dumps({
        "说明": f"海关总署归类决定/裁定（生效中，{min_year} 年起），由 eval/fetch_rulings.py 生成。hs6 取决定税号前 6 位；"
                "决定税号在本地中国税则里存在时附 cn10。",
        "cases": cases,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"生成 {CASES}：{len(cases)} 条；跳过 {len(skipped)} 条")
    for rid, why in skipped[:20]:
        print(f"  跳过 {rid}：{why}")


def main() -> None:
    p = argparse.ArgumentParser(description="抓取海关归类决定，生成中文评测集")
    p.add_argument("--max", type=int, default=120, help="这次最多新抓多少条")
    p.add_argument("--sources", default="全部", help="数据来源，逗号分隔，如 归类决定,归类裁定；默认「全部」一次查出")
    p.add_argument("--min-year", type=int, default=2022, help="只要这一年及以后的决定（HS 2022 版）")
    p.add_argument("--build-only", action="store_true", help="不抓取，只用已有原始内容生成评测集")
    args = p.parse_args()
    if not args.build_only:
        cfg = Config()
        n = asyncio.run(RulingsFetcher(cfg).run(args.sources.split(","), args.max, args.min_year))
        print(f"这次新抓 {n} 条，原始内容在 {RAW}")
    build(args.min_year)


if __name__ == "__main__":
    main()
