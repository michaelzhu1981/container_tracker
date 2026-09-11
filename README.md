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

结果写入 `output/containers_result.xlsx`。失败截图在 `screenshots/`，页面 HTML 在 `logs/html/`。

当前已实现 Hapag-Lloyd (`HLCU`) 与 Yang Ming (`YMJA`)。其余船公司会记为尚未实现。Hapag 若遇到 Cloudflare，该行记失败并继续；阳明站点目前可直接打开。遇到 CAPTCHA / Cloudflare 时该行进入人工队列，程序继续查下一箱。

## License

MIT. See `LICENSE`.
