"""在线查询海关总署「进出口税则商品及品目注释」，结果缓存在本地。

Agent 打开某一章或某个品目时，程序像人一样操作查询页面：
在「税则子目」里输入 4 位品目号 → 点「查询」→ 在列表里点这个品目的「详细」→ 读出类注、章注、品目注释。
查到的内容存进 agent_data/notes_cache/，有效期内（默认 15 天）直接用缓存，过期后重新查询并更新。

点哪里、读什么都写在代码里，不让大模型自己操作浏览器：步骤固定，这样更快、更稳，也不花 token。
网站改版导致某一步找不到元素时，会把当时的页面保存到 agent_data/notes_debug/，方便调整。

单独试用（也可以用来提前把常用品目查好存进缓存）：
  python -m hs_agent.official_notes 8516 8509 --show
  python -m hs_agent.official_notes 8516 --headed      # 显示浏览器窗口，看它一步步操作
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config

MANUAL = "manual"  # 手动导入的注释，url 记为这个值，不会过期
LABELS = {"类注": "section_notes", "章注": "chapter_notes", "品目注释": "heading_notes"}
LABEL_RE = re.compile(r"(?m)^\s*(类注|章注|品目注释)\s*[:：]")


class NotesError(Exception):
    pass


class BlankPage(Exception):
    """页面打开后一直是空白，多半是被网站的反爬虫校验拦下了。"""


@dataclass
class OfficialNotes:
    heading: str
    section_notes: str
    chapter_notes: str
    heading_notes: str
    raw_text: str
    url: str
    fetched_at: float

    @property
    def age_days(self) -> float:
        return (time.time() - self.fetched_at) / 86400


def parse_detail(text: str) -> dict[str, str]:
    """把「详细」页面的文字按「类注：」「章注：」「品目注释：」三个标签切开。找不到标签时整段放进品目注释。"""
    parts = {v: "" for v in LABELS.values()}
    matches = list(LABEL_RE.finditer(text))
    if not matches:
        parts["heading_notes"] = text.strip()
        return parts
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        body = re.sub(r"\n?\s*关\s*闭\s*$", "", body).strip()  # 页面底部的「关闭」按钮
        key = LABELS[m.group(1)]
        parts[key] = (parts[key] + "\n" + body).strip() if parts[key] else body
    return parts


class NotesCache:
    def __init__(self, folder: Path, ttl_days: float):
        self.folder = folder
        self.ttl_days = ttl_days

    def _path(self, heading: str) -> Path:
        return self.folder / f"{heading}.json"

    def get(self, heading: str, allow_stale: bool = False) -> OfficialNotes | None:
        p = self._path(heading)
        if not p.exists():
            return None
        try:
            notes = OfficialNotes(**json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            return None
        if notes.url != MANUAL and notes.age_days > self.ttl_days and not allow_stale:
            return None
        return notes

    def in_chapter(self, chapter: str) -> OfficialNotes | None:
        """这一章里任意一个已有注释的品目（类注、章注同一章都一样）。"""
        for p in sorted(self.folder.glob(f"{chapter}??.json")):
            notes = self.get(p.stem)
            if notes and (notes.section_notes or notes.chapter_notes):
                return notes
        return None

    def put(self, notes: OfficialNotes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(notes.heading).write_text(json.dumps(asdict(notes), ensure_ascii=False, indent=1), encoding="utf-8")


class NotesFetcher:
    """用 Playwright 操作查询页面。浏览器在第一次查询时才启动，整个进程共用一个。

    海关网站有反爬虫校验：打开页面时先下发一段脚本检查浏览器，认定是自动化程序就停在空白页。
    所以优先用电脑上安装的 Google Chrome，去掉自动化标记，并用固定的浏览器资料目录保存校验通过后的 Cookie；
    无窗口模式没通过时，再用窗口模式（窗口放在屏幕外）试一次。
    """

    def __init__(self, config: Config):
        self.config = config
        self._pw = None
        self._context = None
        self._headless = config.notes_headless
        self.browser_name = ""
        self._lock = asyncio.Lock()
        self._last_fetch = 0.0

    async def _ensure_context(self):
        if self._context:
            return self._context
        # patchright 是 Playwright 的改版，去掉了会被反爬虫脚本识别出来的调试痕迹；装了就优先用它
        stealth = True
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            stealth = False
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise NotesError("没有安装 Playwright：pip install playwright && playwright install chromium") from e
        if stealth and self._headless and self.config.notes_window_fallback:
            self._headless = False  # 无窗口模式基本过不了校验，patchright 直接用窗口模式（窗口放在屏幕外）
        if not self._pw:
            self._pw = await async_playwright().start()
        profile = self.config.data_dir / "browser_profile"
        profile.mkdir(parents=True, exist_ok=True)
        if stealth:  # patchright 建议不加额外参数和注入脚本，越接近普通浏览器越好
            args, kwargs = [], dict(headless=self._headless, no_viewport=True)
        else:
            args = ["--disable-blink-features=AutomationControlled"]
            kwargs = dict(headless=self._headless, locale="zh-CN", ignore_default_args=["--enable-automation"],
                          viewport={"width": 1280, "height": 900})
        if not self._headless and self.config.notes_headless:  # 自动换成窗口模式时，把窗口放到屏幕外
            args += ["--window-position=-2400,-2400", "--window-size=1280,900"]
        if args:
            kwargs["args"] = args
        attempts = []
        if self.config.browser_path:
            attempts.append(("指定的浏览器", {"executable_path": self.config.browser_path}))
        else:
            attempts.append(("Google Chrome", {"channel": "chrome"}))
            attempts.append(("自带的 Chromium", {}))
        errors = []
        for name, extra in attempts:
            try:
                self._context = await self._pw.chromium.launch_persistent_context(str(profile), **kwargs, **extra)
                self.browser_name = name + ("（patchright）" if stealth else "（playwright）")
                break
            except Exception as e:
                errors.append(f"{name}：{str(e).splitlines()[0] if str(e) else type(e).__name__}")
        if not self._context:
            raise NotesError("浏览器启动失败（" + "；".join(errors) + "）")
        if not stealth:
            await self._context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return self._context

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def fetch(self, heading: str) -> OfficialNotes:
        async with self._lock:
            # 两次在线查询之间至少隔几秒，控制访问频率
            wait = self._last_fetch + self.config.notes_interval_s - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_fetch = time.time()
            try:
                return await self._fetch_once(heading)
            except BlankPage:
                if not self._headless or not self.config.notes_window_fallback:
                    raise NotesError(
                        f"网站的反爬虫校验没有通过，页面一直是空白（浏览器：{self.browser_name}）。"
                        + self._hint()
                    ) from None
                # 无窗口模式被拦了，换成窗口模式再试一次（窗口放在屏幕外，不打扰）
                await self.close()
                self._headless = False
                try:
                    return await self._fetch_once(heading)
                except BlankPage:
                    raise NotesError(
                        f"网站的反爬虫校验没有通过，页面一直是空白（浏览器：{self.browser_name}）。"
                        + self._hint()
                    ) from None

    def _hint(self) -> str:
        if "patchright" in self.browser_name:
            return "请把 agent_data/notes_debug/ 里最新的 info.txt 发给开发者。"
        return "请先安装 patchright 再试：pip install patchright"

    async def _fetch_once(self, heading: str) -> OfficialNotes:
        context = await self._ensure_context()
        page = await context.new_page()
        page.set_default_timeout(self.config.notes_timeout_ms)
        try:
            return await self._fetch(context, page, heading)
        except (NotesError, BlankPage):
            await self._dump(context, heading)
            raise
        except Exception as e:  # 超时、网络错误等
            await self._dump(context, heading)
            raise NotesError(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}") from e
        finally:
            for p in list(context.pages)[1:]:  # 只留浏览器启动时自带的第一个标签页
                try:
                    await p.close()
                except Exception:
                    pass

    async def _open(self, page, url: str = "") -> None:
        """打开查询页。反爬虫脚本会先放一个空白页，设置好 Cookie 后自动刷新，这里等它刷新出真正的内容。"""
        self.last_status = None
        resp = await page.goto(url or self.config.notes_url, wait_until="domcontentloaded")
        self.last_status = resp.status if resp else None
        deadline = time.time() + self.config.notes_timeout_ms / 1000
        while time.time() < deadline:
            try:
                await page.wait_for_load_state("load", timeout=3000)
            except Exception:
                pass
            for frame in page.frames:
                try:
                    text = await frame.locator("body").inner_text(timeout=1000)
                except Exception:
                    continue
                if text.strip():
                    return
            await page.wait_for_timeout(500)
        raise BlankPage()

    async def _fetch(self, context, page, heading: str) -> OfficialNotes:
        await self._open(page)
        frame, box = await self._find_in_frames(page, self._query_box, "「税则子目」输入框")
        await box.fill(heading)
        button = frame.get_by_role("button", name=re.compile(r"^\s*查\s*询\s*$"))
        if not await button.count():
            button = frame.locator("input[value='查询'], a:has-text('查询'), span:has-text('查询')")
        await button.first.click()

        row = frame.locator("tr").filter(has=frame.locator("td", has_text=re.compile(rf"^\s*{heading}\s*$")))
        try:
            await row.first.wait_for(state="visible")
        except Exception as e:
            raise NotesError(f"查询结果里没有找到品目 {heading} 这一行") from e
        detail = row.first.locator("button, a, input[type=button], span").filter(has_text=re.compile("详细|详情"))
        if not await detail.count():
            detail = row.first.locator("input[value='详细'], input[value='详情']")
        if not await detail.count():
            raise NotesError(f"品目 {heading} 这一行没有找到「详细」按钮")

        # 「详细」可能弹出新窗口，也可能在当前页面里打开弹层或 iframe，三种情况都处理
        pages_before = set(context.pages)
        await detail.first.click()
        text = await self._read_detail(context, page, pages_before)
        if not text:
            raise NotesError("点开「详细」后没有读到类注、章注或品目注释")
        parts = parse_detail(text)
        return OfficialNotes(heading=heading, raw_text=text, url=self.config.notes_url, fetched_at=time.time(), **parts)

    @staticmethod
    async def _query_box(frame):
        box = frame.locator("xpath=//*[contains(normalize-space(text()),'税则子目')]/following::input[not(@type='hidden')][1]")
        return box if await box.count() else None

    async def _find_in_frames(self, page, finder, what: str):
        deadline = time.time() + self.config.notes_timeout_ms / 1000
        while time.time() < deadline:
            for frame in page.frames:
                try:
                    found = await finder(frame)
                except Exception:
                    found = None
                if found:
                    return frame, found.first
            await page.wait_for_timeout(500)
        raise NotesError(f"页面上没有找到{what}")

    async def _read_detail(self, context, page, pages_before: set) -> str:
        deadline = time.time() + self.config.notes_timeout_ms / 1000
        while time.time() < deadline:
            candidates = [p for p in context.pages if p not in pages_before] + [page]
            for p in candidates:
                for frame in p.frames:
                    try:
                        text = await frame.locator("body").inner_text(timeout=2000)
                    except Exception:
                        continue
                    if LABEL_RE.search(text):  # 「类注：」「章注：」「品目注释：」这样的标签，标题里的字样不算
                        return self._detail_part(text)
            await page.wait_for_timeout(500)
        return ""

    @staticmethod
    def _detail_part(text: str) -> str:
        """弹层和列表在同一页面时，只取「类注」开始之后的部分。"""
        m = LABEL_RE.search(text)
        return text[m.start():].strip() if m else text

    async def _dump(self, context, heading: str) -> None:
        folder = self.config.data_dir / "notes_debug"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        info = (f"浏览器：{self.browser_name}，无窗口模式：{self._headless}\n"
                f"查询页 HTTP 状态：{getattr(self, 'last_status', None)}\n"
                + "\n".join(f"page{i}：{p.url}" for i, p in enumerate(context.pages)))
        (folder / f"{stamp}-{heading}-info.txt").write_text(info, encoding="utf-8")
        for i, p in enumerate(context.pages):
            try:
                await p.screenshot(path=str(folder / f"{stamp}-{heading}-page{i}.png"), full_page=True)
                for j, frame in enumerate(p.frames):
                    (folder / f"{stamp}-{heading}-page{i}-frame{j}.html").write_text(await frame.content(), encoding="utf-8")
            except Exception:
                pass


class SharedFetcher:
    """整个程序共用一个浏览器，放在单独的线程里运行。

    同时归类多个商品（网页界面、评测并发）时，如果各自启动 Chrome，会争用同一个浏览器资料目录，
    多出一堆空白标签页，查询也会失败。所以所有查询都交给这一个线程排队执行。
    连续失败几次后暂停在线查询一段时间，避免每个品目都白等。
    """

    _instance: "SharedFetcher | None" = None
    _guard = threading.Lock()
    FAIL_LIMIT = 2
    PAUSE_S = 600

    def __init__(self, config: Config):
        self.fetcher = NotesFetcher(config)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="notes-browser")
        self.thread.start()
        self.failures = 0
        self.paused_until = 0.0
        self.last_error = ""
        atexit.register(self.shutdown)

    @classmethod
    def get(cls, config: Config) -> "SharedFetcher":
        with cls._guard:
            if cls._instance is None:
                cls._instance = cls(config)
            return cls._instance

    async def fetch(self, heading: str) -> OfficialNotes:
        if time.time() < self.paused_until:
            raise NotesError(f"在线查询暂停中（刚才连续失败：{self.last_error}）")
        future = asyncio.run_coroutine_threadsafe(self.fetcher.fetch(heading), self.loop)
        try:
            notes = await asyncio.wrap_future(future)
        except NotesError as e:
            self.failures += 1
            self.last_error = str(e)
            if self.failures >= self.FAIL_LIMIT:
                self.paused_until = time.time() + self.PAUSE_S
                self.failures = 0
            raise
        self.failures = 0
        return notes

    async def close(self) -> None:
        pass  # 共用的浏览器在程序退出时才关

    def shutdown(self) -> None:
        if self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.fetcher.close(), self.loop).result(timeout=10)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)


class OfficialNotesService:
    """先查缓存（含手动导入的注释），过期或没有才在线查询；在线查询失败时退回使用过期缓存。

    NOTES_ONLINE=0 时只读本地已有的注释，不访问网站。
    """

    def __init__(self, config: Config, fetcher: NotesFetcher | None = None):
        self.config = config
        self.cache = NotesCache(config.data_dir / "notes_cache", config.notes_ttl_days)
        self.offline = not config.notes_online and fetcher is None
        self.fetcher = fetcher or (None if self.offline else SharedFetcher.get(config))
        self.disabled_reason = "没有开启在线查询，本地也没有这个品目的注释" if self.offline else ""

    @staticmethod
    def _source(notes: OfficialNotes) -> str:
        if notes.url == MANUAL:
            return "手动导入的官方注释"
        return f"本地缓存，{notes.age_days:.0f} 天前查询"

    async def get_chapter(self, chapter: str, first_heading: str) -> tuple[OfficialNotes | None, str]:
        """类注、章注：本章任一品目已有注释就直接用，没有再查本章第一个品目。"""
        notes = self.cache.in_chapter(chapter)
        if notes:
            return notes, self._source(notes)
        return await self.get(first_heading)

    async def get(self, heading: str) -> tuple[OfficialNotes | None, str]:
        """返回 (注释, 来源说明)。来源：缓存 / 在线 / 过期缓存 / 不可用的原因。"""
        cached = self.cache.get(heading)
        if cached:
            return cached, self._source(cached)
        if self.disabled_reason:
            stale = self.cache.get(heading, allow_stale=True)
            return stale, self.disabled_reason if not stale else f"过期缓存（{self.disabled_reason}）"
        try:
            notes = await self.fetcher.fetch(heading)
        except NotesError as e:
            if "没有安装 Playwright" in str(e):
                self.disabled_reason = str(e)
            stale = self.cache.get(heading, allow_stale=True)
            if stale:
                return stale, f"在线查询失败，使用 {stale.age_days:.0f} 天前的缓存（{e}）"
            return None, f"在线查询失败：{e}"
        self.cache.put(notes)
        return notes, "刚刚在线查询"

    async def close(self) -> None:
        if self.fetcher:
            await self.fetcher.close()


HEADING_RE = re.compile(r"(?<![\d.])(\d{2})\.(\d{2})(?![\d.])")


def guess_heading(text: str) -> str:
    """从「品目注释：」后面的第一个 85.16 这样的编号推断品目号。"""
    start = text.find("品目注释")
    found = HEADING_RE.search(text, start if start != -1 else 0)
    return found.group(1) + found.group(2) if found else ""


def import_text(cache: NotesCache, text: str, heading: str = "") -> OfficialNotes:
    """把从网页「详细」里复制出来的文字存成注释。heading 为空时从文字里推断。"""
    text = text.replace("\r\n", "\n").strip()
    m = LABEL_RE.search(text)
    if m:
        text = text[m.start():]
    heading = re.sub(r"\D", "", heading) or guess_heading(text)
    if len(heading) != 4:
        raise NotesError("认不出是哪个品目，请把文件命名为 4 位品目号，例如 8516.txt")
    parts = parse_detail(text)
    if not (parts["section_notes"] or parts["chapter_notes"]) and parts["heading_notes"] == text:
        raise NotesError(f"{heading}：没有找到「类注：」「章注：」「品目注释：」这些标签，请复制「详细」页面里的全部文字")
    notes = OfficialNotes(heading=heading, raw_text=text, url=MANUAL, fetched_at=time.time(), **parts)
    cache.put(notes)
    return notes


def import_folder(cache: NotesCache, folder: Path) -> list[tuple[str, str]]:
    """导入文件夹里的每个 .txt（一个品目一个文件），返回 [(文件名, 结果)]。"""
    results = []
    for f in sorted(folder.glob("*.txt")):
        try:
            name = f.stem if re.fullmatch(r"\d{4}", f.stem) else ""
            n = import_text(cache, f.read_text(encoding="utf-8", errors="replace"), name)
            results.append((f.name, f"✓ 品目 {n.heading}：类注 {len(n.section_notes)} 字，章注 {len(n.chapter_notes)} 字，品目注释 {len(n.heading_notes)} 字"))
        except NotesError as e:
            results.append((f.name, f"✗ {e}"))
    return results


async def _cli(args) -> None:
    cfg = Config()
    if args.import_dir:
        cache = NotesCache(cfg.data_dir / "notes_cache", cfg.notes_ttl_days)
        results = import_folder(cache, Path(args.import_dir))
        for name, msg in results:
            print(f"{name}：{msg}")
        print(f"共 {len(results)} 个文件，导入到 {cache.folder}" if results else f"{args.import_dir} 里没有 .txt 文件")
        return
    if args.headed:
        cfg.notes_headless = False
    if args.refresh:
        cfg.notes_ttl_days = 0
    service = OfficialNotesService(cfg)
    try:
        for heading in args.headings:
            notes, source = await service.get(heading)
            if not notes:
                print(f"✗ {heading}：{source}")
                continue
            sizes = f"类注 {len(notes.section_notes)} 字，章注 {len(notes.chapter_notes)} 字，品目注释 {len(notes.heading_notes)} 字"
            print(f"✓ {heading}：{source}；{sizes}")
            if args.show:
                for title, body in (("类注", notes.section_notes), ("章注", notes.chapter_notes), ("品目注释", notes.heading_notes)):
                    print(f"\n【{title}】\n{body[:1500]}{'…' if len(body) > 1500 else ''}")
                print()
    finally:
        await service.close()
    print(f"缓存目录：{service.cache.folder}")


def main() -> None:
    p = argparse.ArgumentParser(description="在线查询海关「进出口税则商品及品目注释」")
    p.add_argument("headings", nargs="*", help="4 位品目号，如 8516")
    p.add_argument("--import", dest="import_dir", metavar="文件夹", help="导入手动复制的注释：文件夹里每个品目一个 .txt")
    p.add_argument("--show", action="store_true", help="打印查到的注释内容")
    p.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    p.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新查询")
    asyncio.run(_cli(p.parse_args()))


if __name__ == "__main__":
    main()
