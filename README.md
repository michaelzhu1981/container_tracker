# 集装箱追踪 MVP

本地 Mac 工具：从 Excel 读取箱号和船公司，查询船公司公开 Tracking 页，判断是否已装上海船 / 已开船。

代码、CLI、Excel 表头、状态值均为英文。详细规则见 `design/container_tracking_mvp_plan.md`。

## 船公司

| 代码 | 船公司 | 适配器 |
|---|---|---|
| `HLCU` | Hapag-Lloyd | 已实现；默认打开可见 Chrome |
| `YMJA` | Yang Ming | 已实现；默认无头 |
| `ONEY` | ONE | 已实现；默认无头 |
| `MSCU` | MSC | 已实现；默认打开可见 Chrome |
| `MAEU` | Maersk | 尚未实现，记 `SELECTOR` |
| `CMDU` | CMA CGM | 尚未实现，记 `SELECTOR` |

每家船公司共用一个持久 Chrome 资料目录（`sessions/chrome_{code}/`）。系统已装 Google Chrome 时优先用它，否则退回 Playwright Chromium。HLCU / MSCU 更容易碰到 Cloudflare，因此默认 headed。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 输入

编辑 `input/containers.xlsx`：把箱号贴到 `Container` 列第 2 行起，在 `Carrier` 列填写 `HLCU` / `YMJA` / `ONEY` / `MAEU` / `MSCU` / `CMDU`。不要填写 POL，程序从最近一程海船装船港推断。

也接受表头别名，例如 `箱号` / `集装箱号` / `船公司` / `SCAC`。多余列会原样带到结果表。

读入时按 `Container+Carrier` 去重（保留首行），再按船公司分组、一家查完再查下一家。箱号会去掉空格和连字符并转大写；校验位不对仍会查询，只打日志警告。

## 本地网页控制台

查看每箱状态，并开始 / 结束任务（同一时间只能跑一批；点 Stop 会等当前箱查完）：

```bash
python app.py --serve
```

启动后会尝试打开 `http://127.0.0.1:8765/`。输入仍是 `input/containers.xlsx`，结果写入 `output/containers_result.xlsx`。可用 `--host` / `--port` 改绑定。

页面上可以：

- **Start / Stop / Reload from Excel**：开跑、停在当前箱之后、重新读入输入和已有结果
- **Skip already SAILED**：复用结果表里已经是 `SAILED` 的行，不再查
- **Wait for challenge**：自动等待失败后，把挑战交给本机 Chrome（默认开）。关掉则自动失败并可能熔断该家
- **Show browser**：所有船公司都开可见窗口；关掉时 HLCU / MSCU 仍会开 Chrome
- 按船公司筛选本次要查的家；点 Summary 行或状态计数可过滤表格
- 按船公司看完成进度（含百分比）

网页没有终端。遇到 Cloudflare 交接时：在弹出的 Google Chrome 里完成验证，等到出现箱号搜索框，**关掉该 Chrome**，程序会自动接上同一资料目录继续查。

## 命令行

单箱（目前实现 HLCU / YMJA / ONEY / MSCU）：

```bash
python app.py --carrier HLCU --container HLXU1234567
python app.py --carrier YMJA --container YMLU1234567
python app.py --carrier ONEY --container ONEU1234567
python app.py --carrier MSCU --container MSCU1234567
python app.py --carrier HLCU --container HLXU1234567 --headed
```

批量：

```bash
python app.py input/containers.xlsx
python app.py input/containers.xlsx --carriers HLCU,YMJA --limit 20
python app.py input/containers.xlsx --resume
python app.py input/containers.xlsx --output output/today.xlsx
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--headed` | 强制可见浏览器（调试） |
| `--wait-challenge` | 等人过挑战（已是默认） |
| `--no-wait-challenge` | 无人值守：自动失败后不等人 |
| `--carriers HLCU,YMJA` | 只查这些船公司 |
| `--limit N` | 最多查 N 行（去重、分组之后） |
| `--resume` | 跳过结果表里已是 `SAILED` 的行 |
| `--output PATH` | 结果 Excel 路径 |
| `--serve` | 打开本地网页控制台 |

## 结果

结果写入 `output/containers_result.xlsx`。若该文件正被 Excel 打开，会改写带时间戳的副本。`screenshots/` 只保存箱号查询结果（事件表），不含登录、Cookie 横幅或 Cloudflare 页。完整 HTML 在 `logs/html/`。

输出列：`Container` `Carrier` `POL` `Status` `Loaded` `Sailed` `Vessel` `Voyage` `ATD` `Latest Event` `Checked At` `Check Result` `Error Code` `Error` `Screenshot`。

| Status | 含义 |
|---|---|
| `SAILED` | 已装上海船且已开船。ATD 取本程第一次海船离港；YMJA 的 Actual On Board 也算已开船 |
| `LOADED_WAITING_DEPARTURE` | 已装海船，尚未离港 |
| `NOT_LOADED` | 尚未装上海船 |
| `MANUAL_CHECK_REQUIRED` | 需要人工看页面（如 CAPTCHA、航次切不开） |
| `CHECK_FAILED` | 查询失败 |

Loaded / Sailed 只认 feeder / mother / Vessel 的 Actual 事件。驳船离港不算开船；Planned / ETD 不算。

常见错误码：`CLOUDFLARE` `CAPTCHA` `SELECTOR` `PARSE` `TIMEOUT` `NAVIGATION` `INVALID_INPUT` `UNSUPPORTED_CARRIER` `AMBIGUOUS_JOURNEY`。

同一家连续两箱都是 `CLOUDFLARE` 或 `SELECTOR` 时，停查该家剩余行，其他船公司继续。

## Cloudflare / CAPTCHA

1. 先短等非交互 JS 挑战自己消失
2. 仍在挑战页：关掉自动窗口，用同一资料目录打开你平时的 Google Chrome
3. 在那个窗口里完成验证，等到出现箱号搜索框，**关掉该 Chrome**
4. 命令行模式回到终端按 Enter；网页控制台等 Chrome 关掉后自动继续
5. 不要在自动窗口里点勾，那里经常点了也不过
6. `--no-wait-challenge`：不等人，自动失败后记 `CLOUDFLARE`

HLCU / MSCU 默认就会打开可见 Chrome，不必再加 `--wait-challenge`。无人值守：

```bash
python app.py input/containers.xlsx --no-wait-challenge
```

## 测试

```bash
pytest
```

## License

MIT. See `LICENSE`.
