"""配置：全部从环境变量（或 .env）读取。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取，避免额外依赖。已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path.cwd() / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    # 决策模型（OpenAI 兼容接口，默认 DeepSeek）
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.deepseek.com"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-chat"))

    # 数据：HS 6 位国际税则随项目自带；中国 8/10 位编码和退税率需要自己导入
    hs_file: Path = field(default_factory=lambda: Path(_env("HS_FILE") or str(DATA_DIR / "hs2022.csv")))
    sections_file: Path = field(default_factory=lambda: Path(_env("SECTIONS_FILE") or str(DATA_DIR / "sections.csv")))
    notes_dir: Path = field(default_factory=lambda: Path(_env("NOTES_DIR") or str(DATA_DIR / "notes")))
    cn_file: Path = field(default_factory=lambda: Path(_env("CN_TARIFF_FILE") or str(DATA_DIR / "cn_tariff.csv")))

    # 在线查询海关「进出口税则商品及品目注释」（需要安装 Playwright），查到的结果缓存在本地
    notes_online: bool = field(default_factory=lambda: _env("NOTES_ONLINE", "1") == "1")
    notes_url: str = field(default_factory=lambda: _env(
        "NOTES_URL", "http://www.customs.gov.cn/customs/302427/302442/jckszspjpmzscx/index.html"))
    notes_ttl_days: float = field(default_factory=lambda: float(_env("NOTES_TTL_DAYS", "15")))
    notes_headless: bool = field(default_factory=lambda: _env("NOTES_HEADLESS", "1") == "1")
    notes_interval_s: float = field(default_factory=lambda: float(_env("NOTES_INTERVAL_S", "5")))
    notes_timeout_ms: int = field(default_factory=lambda: int(_env("NOTES_TIMEOUT_MS", "20000")))
    # 本机 Chrome 路径，留空则用 Playwright 自带的浏览器
    browser_path: str = field(default_factory=lambda: _env("BROWSER_PATH", ""))

    # 运行参数
    max_steps: int = field(default_factory=lambda: int(_env("MAX_STEPS", "25")))
    # 置信度低于这个值，结果标记为「建议人工复核」
    review_threshold: float = field(default_factory=lambda: float(_env("REVIEW_THRESHOLD", "0.7")))
    data_dir: Path = field(default_factory=lambda: Path(_env("AGENT_DATA_DIR", "agent_data")))

    # 模型价格（元 / 百万 token），用于估算成本。请按官方最新价格修改。
    price_input_per_m: float = field(default_factory=lambda: float(_env("PRICE_INPUT_PER_M", "2")))
    price_output_per_m: float = field(default_factory=lambda: float(_env("PRICE_OUTPUT_PER_M", "8")))

    def ensure_dirs(self) -> None:
        (self.data_dir / "runs").mkdir(parents=True, exist_ok=True)
