# 美股 AI 买点扫描器

扫描 QQQ、GOOGL、NVDA、AAPL、MSFT 的日线 BOLL、RSI 和 MACD。命中买点后调用兼容 OpenAI Chat Completions 的模型，并将本次所有信号汇总为一封发给自己的 QQ 邮件。

## 本地运行

```powershell
python -m venv .venv-stock
.\.venv-stock\Scripts\python.exe -m pip install -r stock-requirements.txt
$env:AI_API_KEY = "你的 DeepSeek API Key"
$env:QQ_EMAIL = "你的 QQ 邮箱地址"
$env:QQ_AUTH_CODE = "你的 QQ 邮箱 SMTP 授权码（不是 QQ 密码）"
.\.venv-stock\Scripts\python.exe stock_ai_scanner.py
```

默认使用 DeepSeek 官方接口和 `deepseek-flash`；其他兼容接口可另设 `AI_BASE_URL` 和 `AI_MODEL`。测试邮件可运行 `python stock_ai_scanner.py --test-email`，只发送测试邮件，不扫描行情或调用模型。请勿把密钥写进代码、README 或提交记录。

## GitHub Actions

将 `stock_ai_scanner.py`、`stock-requirements.txt` 和 `.github/workflows/daily_scan.yml` 放在公开仓库的默认分支。到仓库 **Settings → Secrets and variables → Actions** 新建 `AI_API_KEY`、`QQ_EMAIL`、`QQ_AUTH_CODE` 三个 repository secrets。先在 QQ 邮箱网页设置中开启 SMTP 服务并获取授权码。工作流每天 UTC 22:30 触发，即次日北京时间 06:30，处理前一个美股交易日的收盘数据。周末、节假日或行情未更新时不会发买点邮件。

到 **Actions → Daily US stock AI scan → Run workflow**，勾选 `test_email` 可仅验证 QQ 邮件；不勾选则运行完整扫描。GitHub 定时任务可能延迟，且公开仓库 60 天无活动后可能自动停用。模型 API 可能产生费用或受额度限制。
