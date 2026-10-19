"""官方注释查询：缓存有效期、页面解析，以及用本地模拟页面跑通「输入 → 查询 → 详细 → 读注释」。

模拟页面仿照海关查询页的结构：查询表单在 iframe 里，「详细」分别以新窗口和页内弹层两种方式打开。
"""

import asyncio
import functools
import shutil
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hs_agent.config import Config
from hs_agent.official_notes import import_folder, NotesCache, NotesError, NotesFetcher, OfficialNotes, OfficialNotesService, parse_detail
from hs_agent.runlog import RunLog
from hs_agent.toolbox import RunState, Toolbox
from tests.test_core import TARIFF
from hs_agent.cn_tariff import CnTariff

MOCK = Path(__file__).parent / "mock_notes"
PORT = 8798
CONTAINER_CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def notes(heading="8516", age_days=0.0):
    return OfficialNotes(heading, "类注文本", "章注文本", "品目注释文本", "raw", "u", time.time() - age_days * 86400)


def test_parse_detail():
    text = "类注：\t注释：一、本类不包括\n章注：\n注释：一、本章不包括\n品目注释：\n85.16 本品目包括\n关 闭"
    p = parse_detail(text)
    assert p["section_notes"].startswith("注释：一、本类") and "本章不包括" in p["chapter_notes"]
    assert p["heading_notes"] == "85.16 本品目包括"
    assert parse_detail("没有标签的文字")["heading_notes"] == "没有标签的文字"


def test_cache_ttl(tmp_path):
    cache = NotesCache(tmp_path, ttl_days=15)
    cache.put(notes(age_days=3))
    assert cache.get("8516").heading_notes == "品目注释文本"
    cache.put(notes(age_days=16))
    assert cache.get("8516") is None and cache.get("8516", allow_stale=True)


class FakeFetcher:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    async def fetch(self, heading):
        self.calls += 1
        if self.fail:
            raise NotesError("网站打不开")
        return notes(heading)

    async def close(self):
        pass


def test_service_uses_cache_then_refreshes(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    fetcher = FakeFetcher()
    svc = OfficialNotesService(cfg, fetcher)
    n, src = asyncio.run(svc.get("8516"))
    assert src == "刚刚在线查询" and fetcher.calls == 1
    n, src = asyncio.run(svc.get("8516"))
    assert src.startswith("本地缓存") and fetcher.calls == 1
    svc.cache.put(notes(age_days=20))  # 过期了就重新查
    asyncio.run(svc.get("8516"))
    assert fetcher.calls == 2


def test_service_falls_back_to_stale_cache(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    svc = OfficialNotesService(cfg, FakeFetcher(fail=True))
    n, src = asyncio.run(svc.get("8516"))
    assert n is None and "网站打不开" in src
    svc.cache.put(notes(age_days=20))
    n, src = asyncio.run(svc.get("8516"))
    assert n and "使用 20 天前的缓存" in src


def test_browse_shows_chapter_notes_once(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.ensure_dirs()
    state = RunState(config=cfg, tariff=TARIFF, cn=CnTariff(tmp_path / "none.csv"), log=RunLog(tmp_path / "runs", "t"),
                     notes=OfficialNotesService(cfg, FakeFetcher()))
    tb = Toolbox(state)
    out = asyncio.run(tb.browse("85"))
    assert "章注文本" in out and "品目注释文本" not in out
    out = asyncio.run(tb.browse("8516"))
    assert "品目 8516 注释" in out and "章注文本" not in out


@pytest.fixture(scope="module")
def mock_site():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(MOCK))
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{PORT}"
    srv.shutdown()


def browser_config(tmp_path, url):
    pytest.importorskip("playwright")
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.notes_url = url
    cfg.notes_timeout_ms = 8000
    cfg.notes_interval_s = 0
    cfg.notes_headless, cfg.notes_window_fallback = True, False  # 测试环境没有显示器
    if Path(CONTAINER_CHROME).exists():
        cfg.browser_path = CONTAINER_CHROME
    elif not shutil.which("chromium"):
        cfg.browser_path = ""
    return cfg


@pytest.mark.parametrize("page", ["index.html", "query.html?mode=modal"])
def test_fetch_from_mock_site(mock_site, tmp_path, page):
    cfg = browser_config(tmp_path, f"{mock_site}/{page}")

    async def run():
        f = NotesFetcher(cfg)
        try:
            return await f.fetch("8516")
        finally:
            await f.close()

    n = asyncio.run(run())
    assert "本类不包括" in n.section_notes
    assert "品目85.09仅包括" in n.chapter_notes
    assert "本品目包括家用电热器具" in n.heading_notes and "关 闭" not in n.heading_notes


def test_fetch_missing_heading_saves_debug(mock_site, tmp_path):
    cfg = browser_config(tmp_path, f"{mock_site}/index.html")
    cfg.notes_timeout_ms = 3000

    async def run():
        f = NotesFetcher(cfg)
        try:
            await f.fetch("9999")
        finally:
            await f.close()

    with pytest.raises(NotesError, match="没有找到品目 9999"):
        asyncio.run(run())
    assert list((tmp_path / "notes_debug").glob("*.html"))


def test_manual_import_never_expires(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "复制的.txt").write_text("进出口税则…\n类注：\n本类不包括\n章注：\n本章不包括\n品目注释：\n85.16 本品目包括\n关 闭", encoding="utf-8")
    (folder / "bad.txt").write_text("随便一段话", encoding="utf-8")
    cache = NotesCache(tmp_path / "cache", ttl_days=15)
    results = dict(import_folder(cache, folder))
    assert results["复制的.txt"].startswith("✓ 品目 8516") and results["bad.txt"].startswith("✗")
    n = cache.get("8516")
    assert n.heading_notes == "85.16 本品目包括" and n.section_notes == "本类不包括"
    n.fetched_at -= 100 * 86400
    cache.put(n)
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.notes_online = False
    svc = OfficialNotesService(cfg)
    svc.cache = cache
    got, src = asyncio.run(svc.get_chapter("85", "8501"))
    assert got.heading == "8516" and src == "手动导入的官方注释"
    assert asyncio.run(svc.get("8509")) == (None, "没有开启在线查询，本地也没有这个品目的注释")


def test_shared_fetcher_serializes_and_pauses(tmp_path, monkeypatch):
    from hs_agent.official_notes import SharedFetcher
    cfg = Config()
    cfg.data_dir = tmp_path
    shared = SharedFetcher(cfg)
    active, peak = [0], [0]

    class Slow:
        fail = False

        async def fetch(self, heading):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.05)
            active[0] -= 1
            if self.fail:
                raise NotesError("400")
            return notes(heading)

        async def close(self):
            pass

    slow = Slow()
    shared.fetcher.fetch = slow.fetch  # 换成假的，不启动浏览器
    shared.fetcher._lock = None

    async def guarded(h):  # NotesFetcher.fetch 自带锁；这里模拟同样的排队
        return await shared.fetch(h)

    lock = asyncio.Lock()
    orig = slow.fetch

    async def locked(h):
        async with lock:
            return await orig(h)

    shared.fetcher.fetch = locked
    # 两个线程各自的事件循环同时查询
    out = []
    ts = [threading.Thread(target=lambda h=h: out.append(asyncio.run(guarded(h)).heading)) for h in ("8516", "8509", "8418")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == ["8418", "8509", "8516"] and peak[0] == 1

    slow.fail = True
    for _ in range(2):
        with pytest.raises(NotesError, match="400"):
            asyncio.run(shared.fetch("8516"))
    with pytest.raises(NotesError, match="暂停"):
        asyncio.run(shared.fetch("8516"))
    shared.shutdown()
