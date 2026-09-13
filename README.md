# 集装箱追踪 MVP

本地 Mac 工具：从 Excel 读取箱号和船公司，查询船公司公开 Tracking 页，判断是否已装上海船 / 已开船。

代码、CLI、Excel 表头均为英文。详细规则见 `design/container_tracking_mvp_plan.md`。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

系统已装 Google Chrome 时，HLCU 会优先用 Chrome 的持久资料目录（`sessions/chrome_hlcu/`），比 Playwright 自带 Chromium 更容易过 Cloudflare 的自动检查。

## 输入

编辑 `input/containers.xlsx`：把箱号贴到 `Container` 列第 2 行起，在 `Carrier` 列填写 `HLCU` / `YMJA` / `ONEY` / `MAEU` / `MSCU` / `CMDU`。不要填写 POL。

## 本地网页控制台

查看每箱查询状态，并开始 / 结束任务（同一时间只能跑一批；点 Stop 会等当前箱查完）：

```bash
python app.py --serve
```

浏览器打开 `http://127.0.0.1:8765/`。输入仍是 `input/containers.xlsx`，结果写入 `output/containers_result.xlsx`。

## 运行

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
```

读入时按 `Container+Carrier` 去重（保留首行），再按船公司分组查询。

结果写入 `output/containers_result.xlsx`。`screenshots/` 只保存箱号查询结果（事件表），不含登录、Cookie 横幅或 Cloudflare 页。完整 HTML 在 `logs/html/`。

当前已实现 Hapag-Lloyd (`HLCU`)、Yang Ming (`YMJA`)、ONE (`ONEY`) 与 MSC (`MSCU`)。Maersk / CMA 会记为尚未实现。

HLCU 与 MSCU 默认打开可见 Chrome（Cookie / CAPTCHA）。ONEY 与 YMJA 默认无头。

遇到 Cloudflare / CAPTCHA 时：

1. 先短等非交互 JS 挑战自己消失
2. 仍在挑战页：关掉自动窗口，用同一资料目录打开你平时的 Google Chrome
3. 在那个窗口里完成验证，等到出现箱号搜索框，**关掉该 Chrome**，回到终端按 Enter
4. 程序重新接上同一资料目录继续查。不要在自动窗口里点勾，那里经常点了也不过
5. `--no-wait-challenge`：不等人，自动失败后记 `CLOUDFLARE`

HLCU 默认就会打开可见 Chrome，不必再加 `--wait-challenge`。无人值守、自动失败后不要等人：

```bash
python app.py input/containers.xlsx --no-wait-challenge
```

同一家连续两箱都是 `CLOUDFLARE` 或 `SELECTOR` 时，停查该家剩余行，其他船公司继续。超时仍记 `CLOUDFLARE`。

## License

MIT. See `LICENSE`.
