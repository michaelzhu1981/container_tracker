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

## 运行

单箱（目前实现 HLCU / YMJA）：

```bash
python app.py --carrier HLCU --container HLXU1234567
python app.py --carrier YMJA --container YMLU1234567
python app.py --carrier HLCU --container HLXU1234567 --headed
```

批量：

```bash
python app.py input/containers.xlsx
```

结果写入 `output/containers_result.xlsx`。`screenshots/` 只保存箱号查询结果（事件表），不含登录、Cookie 横幅或 Cloudflare 页。完整 HTML 在 `logs/html/`。

当前已实现 Hapag-Lloyd (`HLCU`) 与 Yang Ming (`YMJA`)。其余船公司会记为尚未实现。

遇到 Cloudflare / CAPTCHA 时：

1. 先短等非交互 JS 挑战自己消失（真实 Chrome 上常见）
2. 失败则刷新页面再等，最多两次
3. 仍停在挑战页，才弹出可见窗口等人点击（最多约 3 分钟）
4. 点完后同一浏览器继续查后面的箱子，不会每箱重开

HLCU 默认就会打开可见 Chrome，不必再加 `--wait-challenge`。无人值守、自动失败后不要等人：

```bash
python app.py input/containers.xlsx --no-wait-challenge
```

同一家连续两箱都是 `CLOUDFLARE` 或 `SELECTOR` 时，停查该家剩余行，其他船公司继续。超时仍记 `CLOUDFLARE`。

## License

MIT. See `LICENSE`.
