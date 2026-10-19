"""本地网页界面：python -m hs_agent.web，浏览器里输入商品描述、实时看归类过程、回答追问、查看结果。

只用标准库，不需要额外安装；只监听本机 127.0.0.1，别人访问不到。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import threading
import time
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

from pydantic import ValidationError

from . import quotes as quotes_mod
from .agent import RunResult, classify, load_data
from .baselines import make_completer
from .batch import Batch, BatchError, read_table, template_excel
from .duty import Policy
from .landed import Quote, Shipment, calc, check_bill, cn_item, compare
from .us_hts import UsHts
from .history import HistoryError, answer_of, open_history
from .config import Config
from .tariff import Tariff, fmt_code, normalize_code
from .toolbox import no_more_info

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 8 * 1024 * 1024  # 上传清单的大小上限
ANSWER_TIMEOUT = 900  # 追问等用户回答的最长时间（秒）
UNKNOWN = "不知道，请按最常见的情况假设，并写进 missing_info。"

ClassifyFn = Callable[..., Awaitable[RunResult]]


def describe_event(record: dict) -> dict | None:
    """把一条运行日志翻译成界面上的一行进度。"""
    if record.get("event") == "error":
        return {"kind": "error", "text": f"出错：{record.get('error', '')}"}
    if record.get("event") == "reuse":
        names = {"corrected": "人工修正", "confirmed": "人工确认", "agent": "Agent 结果"}
        return {"kind": "done", "text": f"记录库里有相同的商品，沿用记录 #{record['record_id']}（{names.get(record['status'], '')}）",
                "detail": "没有调用大模型。要重新归类，勾选「不用历史记录，重新归类」"}
    if record.get("event") == "precedents":
        recs = record.get("records", [])
        return {"kind": "browse", "text": f"找到 {len(recs)} 条人工确认或修正过的相似记录，作为判例参考",
                "detail": "、".join(f"#{r['id']} {fmt_code(r['code'])}（相似度 {r['score']:.0%}）" for r in recs)}
    if record.get("event") != "action":
        return None
    tool, args, ok, note = record["tool"], record.get("args", {}), record.get("ok", True), record.get("note", "")
    if tool == "search":
        scope = f"（限定第 {args['chapter']} 章）" if args.get("chapter") else ""
        text = f"检索候选：{args.get('keywords', '')}{scope}" + ("" if ok else "，没有结果")
        return {"kind": "search", "text": text}
    if tool == "browse":
        code = args.get("code", "")
        if not ok:
            return {"kind": "warn", "text": f"税则里没有 {code}"}
        if code == "ALL":
            text = "浏览全部 21 类"
        elif len(code) == 2:
            text = f"查看第 {code} 章（含注释）"
        elif len(code) == 4:
            text = f"比较品目 {code} 下的全部子目"
        elif len(code) == 6:
            text = f"核对子目 {fmt_code(code)} 及同级子目"
        else:
            text = f"查看第 {code} 类"
        return {"kind": "browse", "text": text}
    if tool == "official_notes":
        verb = "读取" if ok else "没能读取"
        return {"kind": "browse" if ok else "warn", "text": f"{verb}品目 {args.get('heading', '')} 的海关官方注释",
                "detail": args.get("source", "")}
    if tool == "cn_codes":
        return {"kind": "browse", "text": f"查看 {fmt_code(args.get('hs6', ''))} 下的中国 10 位编码"}
    if tool == "ask_human":
        return {"kind": "ask", "text": f"追问：{args.get('question', '')}", "detail": f"回答：{note}"}
    if tool == "finish":
        if not ok:
            return {"kind": "warn", "text": "提交被校验退回，继续修改", "detail": note}
        conf = args.get("confidence")
        return {"kind": "done", "text": f"提交结果 {fmt_code(args.get('hs6', ''))}" + (f"，置信度 {conf:.0%}" if conf is not None else "")}
    return {"kind": "other", "text": tool}


def result_payload(r: RunResult, tariff: Tariff) -> dict:
    out: dict = {
        "steps": r.steps, "questions": r.questions, "seconds": r.seconds, "cost_yuan": r.cost_yuan,
        "tokens": r.input_tokens + r.output_tokens, "rejections": r.finish_rejections, "log": str(r.log_path),
        "record_id": r.record_id, "reused": r.reused, "precedents": r.precedents, "answer": None,
    }
    a = r.answer
    if a:
        d = a.model_dump()
        d["hs6_fmt"] = fmt_code(a.hs6)
        d["cn_fmt"] = fmt_code(a.cn_code) if a.cn_code else ""
        d["path_nodes"] = [{"label": tariff.label(n), "desc": n.desc} for n in tariff.path(a.hs6)]
        d["alternatives"] = [{"code": fmt_code(c), "desc": tariff.get(c).desc} for c in a.alternatives if tariff.get(c)]
        out["answer"] = d
    return out


@dataclass
class Job:
    id: str
    description: str
    allow_ask: bool
    fresh: bool = False
    created: float = field(default_factory=time.time)
    status: str = "running"  # running / asking / done / error
    events: list[dict] = field(default_factory=list)
    question: str = ""
    result: dict | None = None
    error: str = ""
    _answer: str = ""
    _answered: threading.Event = field(default_factory=threading.Event)

    def on_event(self, record: dict) -> None:
        item = describe_event(record)
        if item:
            item["t"] = record.get("t", 0)
            self.events.append(item)

    async def ask(self, question: str) -> str:
        if not self.allow_ask:
            return await no_more_info(question)
        self._answered.clear()
        self.question, self.status = question, "asking"
        got = await asyncio.to_thread(self._answered.wait, ANSWER_TIMEOUT)
        reply = (self._answer.strip() if got else "") or UNKNOWN
        self.question, self.status = "", "running"
        return reply

    def answer(self, text: str) -> bool:
        if self.status != "asking":
            return False
        self._answer = text
        self._answered.set()
        return True

    def public(self) -> dict:
        return {
            "id": self.id, "description": self.description, "status": self.status, "events": self.events,
            "question": self.question, "result": self.result, "error": self.error,
        }


@dataclass
class BatchJob:
    id: str
    filename: str
    batch: Batch
    status: str = "running"  # running / done / cancelled / error
    error: str = ""
    output: Path | None = None

    def public(self) -> dict:
        b = self.batch
        rows = [{"index": r.index, "description": r.description[:80], "status": r.status,
                 "code": r.values.get("建议编码", ""), "review": r.values.get("需人工复核") == "是",
                 "note": r.values.get("备注", "")} for r in b.rows]
        return {"id": self.id, "filename": self.filename, "status": self.status, "error": self.error,
                "counts": b.counts(), "rows": rows,
                "columns": [b.sheet.headers[i] for i in b.sheet.used_cols],
                "seconds": round((b.finished or time.time()) - b.started, 1)}


class App:
    def __init__(self, config: Config, classify_fn: ClassifyFn = classify, model=None, completer=None):
        self.config = config
        self.completer = completer  # 报价提取用的大模型；测试时传入假的
        self._us: tuple[float, UsHts] | None = None
        self.classify_fn = classify_fn
        self.model = model  # 测试时传入假模型
        self.jobs: dict[str, Job] = {}
        self.batches: dict[str, BatchJob] = {}
        self.tariff, self.cn = load_data(config.hs_file, config.sections_file, config.cn_file)

    def status(self) -> dict:
        notes = [p.name for p in self.config.notes_dir.glob("*.md") if p.name != "README.md"] if self.config.notes_dir.exists() else []
        return {
            "api_key": bool(self.config.llm_api_key) or self.model is not None,
            "model": self.config.llm_model,
            "hs_count": sum(1 for n in self.tariff.nodes.values() if n.level == 6),
            "cn_count": len(self.cn.items),
            "notes": len(notes),
            "history": self.history_db.counts() if self.config.history_enabled else None,
        }

    def start(self, description: str, allow_ask: bool, fresh: bool = False) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], description=description, allow_ask=allow_ask, fresh=fresh)
        self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        try:
            r = asyncio.run(self.classify_fn(
                job.description, self.config, answerer=job.ask, model=self.model, on_event=job.on_event, fresh=job.fresh,
            ))
            job.result = result_payload(r, self.tariff)
            job.error = r.error
            job.status = "done" if r.answer else "error"
            if not r.answer and not job.error:
                job.error = "Agent 没有给出结果。"
        except BaseException as e:  # 包括缺少 Key 时的 SystemExit
            job.error = str(e) or type(e).__name__
            job.status = "error"

    @property
    def history_db(self):
        return open_history(self.config.data_dir / "history.db")

    def review(self, rid: int, action: str, code: str = "", note: str = "") -> dict:
        """人工确认或修正一条记录，返回更新后的记录和对应的结果。"""
        h = self.history_db
        rec = h.confirm(rid) if action == "confirm" else h.correct(rid, code, note, self.tariff, self.cn)
        answer = answer_of(rec, self.tariff, self.cn, self.config.review_threshold)
        for job in self.jobs.values():  # 页面上已有的结果也跟着更新
            if job.result and job.result.get("record_id") == rid:
                job.result["answer"] = self._answer_dict(answer)
        return {"record": rec.public(), "answer": self._answer_dict(answer)}

    def _answer_dict(self, a) -> dict:
        d = a.model_dump()
        d["hs6_fmt"] = fmt_code(a.hs6)
        d["cn_fmt"] = fmt_code(a.cn_code) if a.cn_code else ""
        d["path_nodes"] = [{"label": self.tariff.label(n), "desc": n.desc} for n in self.tariff.path(a.hs6)]
        d["alternatives"] = [{"code": fmt_code(c), "desc": self.tariff.get(c).desc} for c in a.alternatives if self.tariff.get(c)]
        return d

    def start_batch(self, filename: str, data: bytes) -> BatchJob:
        """读清单并在后台逐行归类；读不了时抛出 BatchError。"""
        sheet = read_table(data, filename)
        batch = Batch(sheet, self.config, classify_fn=self.classify_fn, model=self.model,
                      concurrency=self.config.batch_concurrency)
        job = BatchJob(id=uuid.uuid4().hex[:10], filename=filename, batch=batch)
        self.batches[job.id] = job
        threading.Thread(target=self._run_batch, args=(job,), daemon=True).start()
        return job

    def _run_batch(self, job: BatchJob) -> None:
        try:
            asyncio.run(job.batch.run())
            job.status = "cancelled" if job.batch.cancelled else "done"
        except BaseException as e:
            job.error = str(e) or type(e).__name__
            job.status = "error"
        try:  # 结果另存一份到 agent_data/batch/，网页关掉也找得到
            out = self.config.data_dir / "batch" / f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(job.filename).stem}-归类结果.xlsx"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(job.batch.to_excel())
            job.output = out
        except OSError:
            pass

    # ---------- 头程成本 ----------

    @property
    def us_hts(self) -> UsHts:
        """美国税则：文件更新后（比如刚下载完）自动重新读取，不用重启。"""
        f = self.config.us_hts_file
        mtime = f.stat().st_mtime if f.is_file() else 0
        if not self._us or self._us[0] != mtime:
            self._us = (mtime, UsHts(f))
        return self._us[1]

    @property
    def policy(self) -> Policy:
        return Policy(self.config.us_policy_file, self.config.us_exempt_file, self.config.us_301_file)

    @property
    def ledger(self):
        return quotes_mod.open_ledger(self.config.data_dir / "logistics.db")

    def landed_status(self) -> dict:
        hts, pol = self.us_hts, self.policy
        return {"us_hts": {"loaded": hts.loaded, "count": len(hts.lines), **{k: hts.meta.get(k) for k in ("source", "saved", "missing_chapters")}},
                "exempt_count": len(pol.exempt), "s301_count": len(pol.s301), "policy_updated": pol.data.get("updated", ""),
                "records": self.ledger.count(), "api_key": bool(self.config.llm_api_key) or self.completer is not None}

    def lookup(self, code: str) -> dict:
        code = normalize_code(code)
        item = cn_item(self.cn, code) if len(code) in (8, 10) else None
        hts = self.us_hts
        lines = [u.public() for u in hts.under(code[:6])][:80] if hts.loaded and len(code) >= 6 else []
        return {"hs6": fmt_code(code[:6]) if len(code) >= 6 else "", "us_loaded": hts.loaded, "us_lines": lines,
                "cn": {"code": fmt_code(item.code), "name": item.name, "refund_rate": item.refund_rate, "vat_rate": item.vat_rate} if item else None}

    def _complete(self):
        if self.completer:
            return self.completer
        if not self.config.llm_api_key:
            raise quotes_mod.QuoteError("没有配置大模型 API Key，不能自动识别。可以用报价模板填好后上传。")
        return make_completer(self.config)

    def extract_quotes(self, body: dict) -> dict:
        shipment = None
        if body.get("shipment"):
            try:
                shipment = Shipment.model_validate(body["shipment"])
            except ValidationError:
                shipment = None
        text = str(body.get("text") or "")
        if body.get("data"):
            data = base64.b64decode(str(body["data"]), validate=True)
            found, text = quotes_mod.read_file(data, str(body.get("filename") or "报价.xlsx"))
            if found:
                return {"quotes": [q.model_dump() for q in found], "via": "template", "cost_yuan": 0}
        found, i, o = asyncio.run(quotes_mod.extract(text, shipment, self._complete()))
        cost = round((i * self.config.price_input_per_m + o * self.config.price_output_per_m) / 1_000_000, 4)
        return {"quotes": [q.model_dump() for q in found], "via": "ai", "cost_yuan": cost}

    def calc_landed(self, body: dict) -> dict:
        shipment = Shipment.model_validate(body["shipment"])
        quotes = [Quote.model_validate(q) for q in body.get("quotes") or []]
        if not quotes:
            raise ValueError("请先添加至少一个报价。")
        for k, q in enumerate(quotes):
            q.id = q.id or f"q{k + 1}"
        hts, pol = self.us_hts, self.policy
        results = [calc(shipment, q, hts, pol, self.cn) for q in quotes]
        return {"results": results, "compare": compare(results, quotes, shipment.settings.daily_profit_cny)}

    def check_bill(self, body: dict) -> dict:
        shipment = Shipment.model_validate(body["shipment"])
        quote = Quote.model_validate(body["quote"])
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("请粘贴账单内容。")
        bill = quotes_mod.parse_bill_lines(text)
        cost = 0.0
        if bill is None:
            bill, i, o = asyncio.run(quotes_mod.extract_bill(text, self._complete()))
            cost = round((i * self.config.price_input_per_m + o * self.config.price_output_per_m) / 1_000_000, 4)
        result = calc(shipment, quote, self.us_hts, self.policy, self.cn)
        checked = check_bill(result, bill, shipment.settings.usd_cny)
        rid = self.ledger.add(shipment, quote, result, bill, checked) if body.get("save", True) else 0
        return dict(checked, record_id=rid, cost_yuan=cost)

    def history(self) -> list[dict]:
        jobs = sorted(self.jobs.values(), key=lambda j: -j.created)[:30]
        out = []
        for j in jobs:
            a = (j.result or {}).get("answer") or {}
            out.append({"id": j.id, "description": j.description, "status": j.status,
                        "hs6": a.get("hs6_fmt", ""), "confidence": a.get("confidence")})
        return out


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 不在终端刷请求日志
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _file(self, data: bytes, name: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f"attachment; filename=\"result.xlsx\"; filename*=UTF-8''{quote(name)}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD * 4 // 3 + 4096:
                self.rfile.read(n)
                return {"_too_big": True}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(app.status())
            if path == "/api/jobs":
                return self._json(app.history())
            if path == "/api/history":
                from urllib.parse import parse_qs
                qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                recs = app.history_db.list(qs.get("q", [""])[0].strip(), qs.get("status", [""])[0], 200)
                return self._json({"records": [r.public() for r in recs], "counts": app.history_db.counts()})
            if path == "/api/landed/status":
                return self._json(app.landed_status())
            if path == "/api/landed/lookup":
                from urllib.parse import parse_qs
                qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                return self._json(app.lookup(qs.get("code", [""])[0]))
            if path == "/api/landed/records":
                return self._json({"records": app.ledger.list(), "count": app.ledger.count()})
            if path == "/api/quotes/template":
                return self._file(quotes_mod.template_excel(), "货代报价模板.xlsx")
            if path == "/api/batch/template":
                return self._file(template_excel(), "商品清单模板.xlsx")
            if path.startswith("/api/batch/"):
                parts = path.split("/")
                job = app.batches.get(parts[3]) if len(parts) > 3 else None
                if not job:
                    return self._json({"error": "not found"}, 404)
                if len(parts) > 4 and parts[4] == "download":
                    return self._file(job.batch.to_excel(), f"{Path(job.filename).stem}-归类结果.xlsx")
                return self._json(job.public())
            if path.startswith("/api/jobs/"):
                job = app.jobs.get(path.rsplit("/", 1)[-1])
                return self._json(job.public()) if job else self._json({"error": "not found"}, 404)
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._body()
            if path == "/api/classify":
                desc = str(body.get("description", "")).strip()
                if not desc:
                    return self._json({"error": "请填写商品描述"}, 400)
                job = app.start(desc[:2000], bool(body.get("allow_ask", True)), bool(body.get("fresh", False)))
                return self._json({"id": job.id})
            if path.startswith("/api/history/") and path.rsplit("/", 1)[-1] in ("confirm", "correct"):
                parts = path.split("/")
                try:
                    out = app.review(int(parts[3]), parts[4], str(body.get("code", "")), str(body.get("note", ""))[:500])
                except (HistoryError, ValueError) as e:
                    return self._json({"error": str(e)}, 400)
                return self._json(out)
            landed = {"/api/quotes/extract": app.extract_quotes, "/api/landed/calc": app.calc_landed,
                      "/api/landed/bill": app.check_bill}
            if path in landed:
                if body.get("_too_big"):
                    return self._json({"error": "文件太大了（上限 8MB）。"}, 400)
                try:
                    return self._json(landed[path](body))
                except (quotes_mod.QuoteError, ValueError, KeyError, TypeError) as e:
                    msg = str(e)
                    if isinstance(e, ValidationError):
                        msg = "填写的内容有误：" + "；".join(f"{'.'.join(map(str, x['loc']))} {x['msg']}" for x in e.errors()[:5])
                    return self._json({"error": msg or type(e).__name__}, 400)
                except Exception as e:  # 大模型接口出错等
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            if path == "/api/batch":
                if body.get("_too_big"):
                    return self._json({"error": "文件太大了（上限 8MB），请分成几个文件。"}, 400)
                try:
                    data = base64.b64decode(str(body.get("data", "")), validate=True)
                except ValueError:
                    return self._json({"error": "文件内容读取失败，请重新选择文件。"}, 400)
                try:
                    job = app.start_batch(str(body.get("filename", "清单.xlsx")), data)
                except BatchError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json(job.public())
            if path.startswith("/api/batch/") and path.endswith("/cancel"):
                job = app.batches.get(path.split("/")[3])
                if not job:
                    return self._json({"error": "not found"}, 404)
                job.batch.cancel()
                return self._json({"ok": True})
            if path.startswith("/api/jobs/") and path.endswith("/answer"):
                job = app.jobs.get(path.split("/")[3])
                if not job:
                    return self._json({"error": "not found"}, 404)
                ok = job.answer(str(body.get("answer", "")))
                return self._json({"ok": ok}, 200 if ok else 409)
            self._json({"error": "not found"}, 404)

    return Handler


def serve(app: App, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return server


def already_running(port: int) -> bool:
    """端口被占用时，判断是不是另一个已经在运行的归类助手。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
            return "hs_count" in json.loads(r.read())
    except Exception:
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="HS 归类 Agent 网页界面")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = p.parse_args()
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = serve(App(Config()), port)
            break
        except OSError:
            if already_running(port):  # 另一个归类助手已经在这个端口运行
                url = f"http://127.0.0.1:{port}/"
                print(f"HS 归类助手已经在运行了，直接打开：{url}")
                if not args.no_browser:
                    webbrowser.open(url)
                return
            # 端口被别的程序占用，试下一个
    if server is None:
        print(f"端口 {args.port}~{args.port + 19} 都被占用了，请用 --port 指定一个空闲端口。")
        return
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"HS 归类助手已启动：{url}\n按 Ctrl+C 退出。")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
        server.server_close()


if __name__ == "__main__":
    main()
