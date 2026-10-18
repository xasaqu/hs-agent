# HS 编码归类与出口退税 Agent

输入一段中文商品描述，它会像归类专员一样，在 HS 税则上按「章 → 品目 → 子目」一层层比较，给出 6 位 HS 编码、每一层的选择理由和依据的归类总规则，以及一个置信度。信息不够时它会追问，没有把握时会标记「建议人工复核」。导入中国税则数据后，还能继续选到 10 位编码，并由程序查出出口退税率和监管条件。

结果仅供参考，不构成归类依据，正式结论以海关审核或预裁定为准。

## 和「直接问 AI」有什么不同

| | 直接问大模型 | 本项目 |
| --- | --- | --- |
| 编码从哪来 | 模型凭记忆写，可能是不存在的编码 | 只能从税则树里选，代码校验必须真实存在 |
| 怎么选出来的 | 一句话理由 | 每一层都列出同级候选、写明为什么选这个、排除那个 |
| 退税率 | 模型凭记忆说，经常是旧的 | 程序查本地数据表，标注数据版本 |
| 不确定时 | 照样给一个很自信的答案 | 追问关键要素；置信度低就建议人工复核 |
| 效果 | 说不清 | 有评测集，和两种对照方法比准确率、校准度、成本 |

## 工作流程

```
商品描述 → 提取归类要素（缺关键信息就追问）
        → search：英文关键词检索候选子目
        → browse：打开章（看注释）→ 打开品目，比较所有同级子目
        → finish：提交编码、逐层理由、依据规则、置信度
        → 代码校验（编码存在？品目看过了？10 位编码合法？）→ 不通过就退回让 Agent 改
        → 程序查表：退税率、监管条件
```

| 模块 | 文件 | 做什么 |
| --- | --- | --- |
| 税则树和检索 | `hs_agent/tariff.py` | HS 2022 全部 21 类、96 章、1228 个品目、5612 个子目；BM25 关键词检索 |
| 工具和校验 | `hs_agent/toolbox.py` | search、browse、cn_codes、ask_human、finish；所有硬性约束都写在代码里 |
| 归类规则 | `hs_agent/rules.py` | 归类总规则六条（写进提示词）；类注、章注从 `data/notes/` 读取 |
| 中国税则 | `hs_agent/cn_tariff.py`、`import_refund.py` | 8/10 位编码、出口退税率、征税率；从出口退税率文库导入 |
| 官方注释 | `hs_agent/official_notes.py` | 在线查询海关类注、章注、品目注释，本地缓存 15 天 |
| 编排 | `hs_agent/agent.py` | OpenAI Agents SDK，接 DeepSeek；finish 通过校验才结束 |
| 网页界面 | `hs_agent/web.py`、`hs_agent/static/index.html` | 本地网页：实时显示过程、页面内回答追问、结果卡片；只用标准库 |
| 对照组 | `hs_agent/baselines.py` | 直接问模型；检索 + 一次选择 |
| 评测 | `eval/` | 种子评测集、官方裁定案例下载、三组对比评测 |

## 安装

需要 Python 3.10 或更高版本。

```bash
cd hs-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # 在线查询海关注释用的浏览器内核
cp .env.example .env          # 然后填上 DeepSeek 的 API Key
```

也可以直接双击 `start.command`，它会自动完成以上步骤并打开网页。

## 使用

### 网页界面（推荐）

```bash
python -m hs_agent.web
```

浏览器会自动打开 http://127.0.0.1:8790/ （端口被占用时会自动换成下一个空闲端口，以终端里显示的地址为准）。在页面里填写商品描述，点「开始归类」，就能实时看到 Agent 检索了什么、比较了哪些品目；它需要追问时，页面上会出现问题框，直接回答即可。结果卡片会显示编码、置信度、逐层选择的理由、依据的规则和其他可能的编码，右侧保留本次打开后的归类记录。

网页只在本机运行，别人访问不到。关闭终端窗口或按 Ctrl+C 即可退出。

### 命令行

```bash
python -m hs_agent "家用电热空气炸锅，5L，塑料外壳，额定功率 1500W"
python -m hs_agent "女式运动 T 恤" --no-ask      # 不追问，直接按最合理的假设归类
python -m hs_agent "电动牙刷" --json             # JSON 输出，方便接入其他系统
```

描述写得越具体越准：商品名称、材质及占比、主要用途、工作原理（是否电动、电热）、是否成套销售。

每次运行的完整推理过程在 `agent_data/runs/*.jsonl`：调用了哪些工具、看了哪些品目、被校验退回过几次。

## 评测

```bash
python -m eval.run_eval                              # 56 条种子集，三种方法都跑
python -m eval.run_eval --modes agent --limit 10     # 先小规模试跑
python -m eval.run_eval --only e-toothbrush,drone    # 只跑指定商品
```

三种方法用同一个模型：

- **逐层下钻 Agent**：本项目
- **检索 + 一次选择**：先检索出 20 个候选子目，再让模型选一个
- **直接问模型**：大多数人「用 AI 查 HS 编码」的方式

`agent_data/eval/<时间>/summary.md` 里有：章、品目、子目三级准确率，编码不存在的比例，高置信和低置信时各自的准确率（检验置信度靠不靠谱），按难度分组的准确率，平均步数、耗时和成本，以及 Agent 判错的商品清单和对应日志。

### 评测集

- `eval/seed_cases.json`：56 条中文商品描述和 HS 2022 版 6 位答案，按难度分为易、中、难。**这份答案是我按税则条文整理的，正式对外引用前请自己核对一遍。**「难」的那几条（电动牙刷、无人机、智能手表、办公转椅、羽绒服等）是专门挑的容易归错的商品。
- 美国海关官方归类裁定（CROSS）：运行下面的命令，下载 ATLAS 论文公开的 200 条测试集，转成英文评测集。裁定给的是美国 10 位编码，前 6 位是国际通用的 HS 编码。这个数据集没有注明许可证，只用于个人评测，不要再分发。

  ```bash
  python -m eval.fetch_cross
  python -m eval.run_eval --cases eval/cross_cases.json
  ```

  ATLAS 论文报告了几个通用大模型在这 200 条上的 6 位准确率：GPT-5-Thinking 55.5%，DeepSeek-R1 26.5%。你的结果可以直接和它对比。

- 中国海关的「商品归类决定」和预裁定公告是最有说服力的中文评测来源。建议从海关总署网站整理 100 到 200 条，格式同 `seed_cases.json`（有 10 位编码就加一个 `cn10` 字段，评测会额外统计 10 位准确率）。

## 中国 10 位编码和出口退税率

项目已附带 `hs_agent/data/cn_tariff.csv`，由国家税务总局「出口退税率文库」2026C 版导入，取 2026-10-18 当天有效的税率：10322 个 8 位或 10 位编码，覆盖 HS 2022 的全部子目。每条记录包括出口退税率、增值税征税率；按征税率分档退税的编码、已公布但尚未执行的税率调整，都写在说明里，结果中会提示。

Agent 浏览 6 位子目时会看到下面的中国编码并从中选择，退税率由程序按编码查表填写，模型不参与。

文库更新后重新导入：

```bash
pip install dbfread
python -m hs_agent.import_refund 解压后的文件夹 --version 2026D    # 文件夹里要有 cmcode.dbf
```

## 海关官方注释（在线查询 + 本地缓存）

类注、章注、品目注释对准确率影响很大，很多「看起来像 A 品目、其实归 B 品目」的情况都由注释规定。Agent 打开某一章或某个品目时，程序会自动去海关总署「[进出口税则商品及品目注释查询](http://www.customs.gov.cn/customs/302427/302442/jckszspjpmzscx/index.html)」查询：在「税则子目」输入 4 位品目号，点「查询」，再点这一行的「详细」，读出类注、章注和品目注释给 Agent 看。

- 点哪里、读什么都写在代码里（`hs_agent/official_notes.py`，用 Playwright 操作浏览器），不让大模型自己操作网页，更快更稳，也不花 token。
- 查到的结果存在 `agent_data/notes_cache/`。15 天内再用到同一个品目直接读缓存，超过 15 天重新查询并更新（`.env` 里 `NOTES_TTL_DAYS` 可改）。
- 在线查询失败时，有旧缓存就用旧缓存，没有就照常按条文和总规则判断，并在过程里注明。
- 同一章的类注、章注只给 Agent 看一次，避免重复占用上下文。

单独试用，或者提前把常用品目查好存进缓存：

```bash
python -m hs_agent.official_notes 8516 8509 --show       # 打印查到的注释
python -m hs_agent.official_notes 8516 --headed          # 显示浏览器窗口，看它一步步操作
python -m hs_agent.official_notes 8516 --refresh         # 忽略缓存，强制重新查询
```

海关网站有反爬虫校验，会把自动化浏览器挡在空白页。程序用 patchright（去掉了调试痕迹的 Playwright 改版）启动电脑上装的 Google Chrome，并把校验通过后的 Cookie 存在 `agent_data/browser_profile/`；无窗口模式被挡时，自动换成窗口模式（窗口放在屏幕外）再试一次。如果还是空白，可以在 `.env` 里设置 `BROWSER_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`。

为了控制访问频率，两次在线查询之间至少间隔 5 秒（`.env` 里 `NOTES_INTERVAL_S` 可改），加上 15 天缓存，实际访问量很低。

也可以不在线查询，改为手动导入：用自己的浏览器打开某个品目的「详细」页面，全选复制文字，存成 `8516.txt` 这样的文件放进一个文件夹，然后运行 `python -m hs_agent.official_notes --import 文件夹`。手动导入的注释不会过期；在 `.env` 里设置 `NOTES_ONLINE=0` 就只用本地注释、不访问网站。

如果网站改版导致某一步找不到，程序会把当时的页面截图和 HTML 存到 `agent_data/notes_debug/`，据此调整 `official_notes.py` 里的定位方式即可。

也可以手工补充注释：把文本保存为 `hs_agent/data/notes/chapter_85.md`、`section_XVI.md`，Agent 打开这一章或这一类时会一并读到。

## 测试

```bash
pip install pytest
python -m pytest -q
```

测试不调用大模型，也不访问外网，覆盖官方注释查询（用本地模拟页面跑通输入、查询、点详细、读注释，以及缓存过期）、网页接口（提交、追问、取结果）、税则树、检索、提交校验、中国税则查表、对照组和评测打分，并用一个「假模型」跑通整条 Agent 链路，验证校验不通过时会退回让 Agent 继续修改。

## 数据来源

- HS 2022 税则（6 位，英文）：UN Comtrade 整理，[datasets/harmonized-system](https://github.com/datasets/harmonized-system)，ODC-PDDL 公共领域许可。
- 中国 8/10 位编码和出口退税率：国家税务总局出口退税率文库 2026C。
- 类注、章注、品目注释：海关总署「进出口税则商品及品目注释查询」，运行时在线查询，不随项目分发。
- 归类总规则：按《进出口税则》归类总规则概括，正式文本以税则为准。
