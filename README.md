# 集装箱追踪 MVP

本地 Mac 工具：从 Excel 读取箱号和船公司，查询船公司公开 Tracking 页，判断是否已装上海船 / 已开船。

代码、CLI、Excel 表头、状态值均为英文。详细规则见 `design/container_tracking_mvp_plan.md`。

## 船公司

| 代码 | 船公司 | 适配器 |
|---|---|---|
| `HLCU` | Hapag-Lloyd | 已实现；用本机普通 Chrome（不用 Playwright）过 Cloudflare 后批量查箱 |
| `YMJA` | Yang Ming | 已实现；默认无头 |
| `ONEY` | ONE | 已实现；默认无头 |
| `MSCU` | MSC | 已实现；默认打开可见 Chrome（无头会被拦） |
| `MAEU` | Maersk | 已实现；复用 Tracking 页、表单查询、真实验证码在当前窗口等待完成 |
| `CMDU` | CMA CGM | 已实现；用本机普通 Chrome（不用 Playwright）过人机验证后批量查箱。还箱超过约 15 天可能无结果 |
| `OOLU` | OOCL | 已实现；用本机普通 Chrome 过人机验证。每箱会新开结果页签，查完关掉后回到入口页再查下一箱 |
| `HDMU` | HMM | 已实现；用本机普通 Chrome 过 Access Denied / abnormal connection 后批量查箱。箱号前缀可以不是 HDMU，Carrier 仍填 `HDMU` |
| `COSU` | COSCO | 已实现；默认无头。解析 SCCT 动态节点表 |
| `ZIMU` | ZIM | 已实现；用本机普通 Chrome 过 hCaptcha 后批量查箱。只走 Track a Shipment 表单，不用箱号深链 |

每家船公司共用一个持久 Chrome 资料目录（`sessions/chrome_{code}/`）。系统已装 Google Chrome 时优先用它，否则退回 Playwright Chromium。HLCU 更容易碰到 Cloudflare；MSCU / MAEU / CMDU / OOLU / ZIMU / HDMU 无头更容易被拦，因此这些家默认 headed。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 输入

编辑 `input/containers.xlsx`：把箱号贴到 `Container` 列第 2 行起，在 `Carrier` 列填写 `HLCU` / `YMJA` / `ONEY` / `MAEU` / `MSCU` / `CMDU` / `OOLU` / `HDMU` / `COSU` / `ZIMU`。不要填写 POL，程序从最近一程海船装船港推断。Carrier 以本列为准，不用箱号前缀猜船公司（例如 HMM 箱号可以是 `DFSU...`，Carrier 仍填 `HDMU`）。

也接受表头别名，例如 `箱号` / `集装箱号` / `船公司` / `SCAC`。多余列会原样带到结果表。

读入时按 `Container+Carrier` 去重（保留首行），再按船公司分组。`ONEY` / `YMJA` / `COSU` 若在输入中则并行（默认可无头），同时有头船公司按 `OOLU` → `MSCU` → `MAEU` → `HLCU` → `CMDU` → `HDMU` → `ZIMU` 串行，互不阻塞。每家使用一个独立 worker 并按顺序逐箱查询；箱号会去掉空格和连字符并转大写，校验位不对仍会查询，只打日志警告。

## 本地网页控制台

查看每箱状态，并开始 / 结束任务（同一时间只能跑一批；点 Stop 会等当前箱查完）：

```bash
python app.py --serve
```

启动后会尝试打开 `http://127.0.0.1:8765/`。输入仍是 `input/containers.xlsx`，结果写入 `output/containers_result.xlsx`。可用 `--host` / `--port` 改绑定。

页面上可以：

- **Start / Stop / Reload from Excel**：开跑、停在当前箱之后、重新读入输入和已有结果
- **Skip already SAILED**：复用结果表里已经是 `SAILED` 的行，不再查
- **Wait for challenge**：自动等待失败后，等待人工验证（默认开）。HLCU / CMDU / OOLU / ZIMU / HDMU 打开本机普通 Chrome，等人过验证后保持窗口打开并批量查该家剩余箱；MAEU 在当前自动化窗口等待；MSCU 交给普通系统 Chrome 后需退出再继续。关掉则自动失败并可能熔断该家
- **Show browser**：所有船公司都开可见窗口；关掉时 HLCU / MSCU / MAEU / CMDU / OOLU / ZIMU / HDMU 仍会开 Chrome
- 按船公司筛选本次要查的家；点 Summary 行或状态计数可过滤表格
- 按船公司看完成进度（含百分比）

网页会显示验证码等待状态和对应操作。HLCU / CMDU / OOLU / ZIMU / HDMU：在本机普通 Chrome 里完成验证并保持窗口打开，通过后按箱批量查询，等待中可点 Stop。OOLU 每箱会打开结果页签，查完后关掉该页签，回到 Cargo Tracking 入口页输入下一箱（后续箱通常不再验证）。MAEU 在当前自动化窗口等待。MSCU 交接到普通系统 Chrome 时：完成验证、出现搜索框后 **Cmd+Q**，程序重新打开同一资料目录查询。

## 命令行

单箱（10 家均已实现）：

```bash
python app.py --carrier HLCU --container HLXU1234567
python app.py --carrier YMJA --container YMLU1234567
python app.py --carrier ONEY --container ONEU1234567
python app.py --carrier MSCU --container MSCU1234567
python app.py --carrier MAEU --container MSKU1234567
python app.py --carrier CMDU --container CMAU1234567
python app.py --carrier OOLU --container OOLU6895702
python app.py --carrier HDMU --container DFSU7369437
python app.py --carrier COSU --container CSNU6609294
python app.py --carrier ZIMU --container TCNU3698035
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

结果写入 `output/containers_result.xlsx`，默认每完成 5 箱或每 10 秒批量保存一次，停止或结束任务时强制保存。若该文件正被 Excel 打开，会改写带时间戳的副本。`screenshots/` 只保存箱号查询结果（事件表），不含登录、Cookie 横幅或 Cloudflare 页。HLCU / CMDU / OOLU / ZIMU 都拍本机 Chrome 真实窗口，不再用手动画页面。完整 HTML 在 `logs/html/`。

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

同一家连续两箱出现 `CLOUDFLARE` / `CAPTCHA` / `SELECTOR` 时，停查该家剩余行，其他船公司继续。

## Cloudflare / CAPTCHA

1. 只识别验证提示或可见验证 iframe；Cookie 说明里的 `.hcaptcha.com`、SDK 脚本和隐藏组件不算挑战。
2. 先等最多 25 秒，让非交互 JS 挑战自行消失。
3. HLCU / CMDU / OOLU / ZIMU / HDMU 打开本机普通 Google Chrome（无远程调试）。在该窗口完成 Cloudflare / DataDome / CAPTCHA / hCaptcha / HMM Access Denied，**不要关闭入口窗口**。最多等 10 分钟；通过后批量查箱。OOLU 每箱的结果页签查完后会关掉，下一箱从入口页继续。若 Chrome 提示，打开 **查看 → 开发者 → 允许 Apple 事件中的 JavaScript**。MAEU 仍在当前自动化窗口等待。Stop 可取消等待。
4. MSCU 仍使用普通系统 Chrome 交接：完成验证、出现搜索框后退出该 Chrome，程序重新打开同一资料目录查询。
5. 每箱最多进入一次人工验证流程；验证后再次被拦则记录失败。无人值守遇到 CAPTCHA 不反复刷新；连续两箱被拦会停止该家剩余查询。

真实 CAPTCHA 仍需要人工完成；保留窗口不能保证站点放行。如果 CMDU 验证框显示“没有互联网接入”，应检查本机网络、代理 / VPN 或联系船公司支持。完全无人值守可使用 CMA CGM 官方 [Visibility API](https://api-portal.cma-cgm.com/products/visibility)，其公共接口也需要申请 API Key；本工具目前仍使用网页查询。

HLCU / MSCU / MAEU / CMDU / OOLU / ZIMU / HDMU 默认就会打开可见 Chrome，不必再加 `--wait-challenge`。HLCU / CMDU / MAEU / ZIMU 不再走 GET search 或箱号深链（更容易再次触发挑战）。无人值守：

```bash
python app.py input/containers.xlsx --no-wait-challenge
```

## 测试

```bash
pytest
```

## License

MIT. See `LICENSE`.
