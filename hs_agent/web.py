"""本地网页界面：python -m hs_agent.web，浏览器里输入商品描述、实时看归类过程、回答追问、查看结果。

只用标准库，不需要额外安装；只监听本机 127.0.0.1，别人访问不到。
"""

from __future__ import annotations

import argparse
import asyncio
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

from .agent import RunResult, classify, load_data
from .config import Config
from .tariff import Tariff, fmt_code
from .toolbox import no_more_info

STATIC = Path(__file__).parent / "static"
ANSWER_TIMEOUT = 900  # 追问等用户回答的最长时间（秒）
UNKNOWN = "不知道，请按最常见的情况假设，并写进 missing_info。"

ClassifyFn = Callable[..., Awaitable[RunResult]]


def describe_event(record: dict) -> dict | None:
    """把一条运行日志翻译成界面上的一行进度。"""
    if record.get("event") == "error":
        return {"kind": "error", "text": f"出错：{record.get('error', '')}"}
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
        "answer": None,
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


class App:
    def __init__(self, config: Config, classify_fn: ClassifyFn = classify, model=None):
        self.config = config
        self.classify_fn = classify_fn
        self.model = model  # 测试时传入假模型
        self.jobs: dict[str, Job] = {}
        self.tariff, self.cn = load_data(config.hs_file, config.sections_file, config.cn_file)

    def status(self) -> dict:
        notes = [p.name for p in self.config.notes_dir.glob("*.md") if p.name != "README.md"] if self.config.notes_dir.exists() else []
        return {
            "api_key": bool(self.config.llm_api_key) or self.model is not None,
            "model": self.config.llm_model,
            "hs_count": sum(1 for n in self.tariff.nodes.values() if n.level == 6),
            "cn_count": len(self.cn.items),
            "notes": len(notes),
        }

    def start(self, description: str, allow_ask: bool) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], description=description, allow_ask=allow_ask)
        self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        try:
            r = asyncio.run(self.classify_fn(
                job.description, self.config, answerer=job.ask, model=self.model, on_event=job.on_event,
            ))
            job.result = result_payload(r, self.tariff)
            job.error = r.error
            job.status = "done" if r.answer else "error"
            if not r.answer and not job.error:
                job.error = "Agent 没有给出结果。"
        except BaseException as e:  # 包括缺少 Key 时的 SystemExit
            job.error = str(e) or type(e).__name__
            job.status = "error"

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

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
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
                job = app.start(desc[:2000], bool(body.get("allow_ask", True)))
                return self._json({"id": job.id})
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
