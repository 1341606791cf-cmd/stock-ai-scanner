# 美股低吸评分与 AI 邮件提醒

每个北京时间工作日（周一至周五）06:30 扫描 **QQQ、GOOGL、NVDA、AAPL、MSFT、ASML（阿斯麦）、COST（开市客）**，并发送一封 QQ 汇总邮件。
只有新完整日线的总分 **>=70** 才调用 DeepSeek；没有达标标的也发“今日无70分及以上标的”，无需调用 AI。
周一或美股休市、行情延迟时仍发送状态邮件，标注实际数据日期；旧评分仅供参考，不重复调用 AI。按北京时间工作日发送，因此北京时间周六不发送周五美股的收盘报告。
70–79 分标为“轻仓试探”，80–100 分标为“黄金坑候选”。评分表示技术规则匹配程度，不代表实际胜率。

## 100 分评分规则

每个维度只取最高命中档，三个维度相加，不重复累计。

| 维度 | QQQ 指数档 | 其他六只个股档 |
|---|---|---|
| BOLL，满分40 | 跌破下轨40；距下轨1%以内30；跌破中轨15；其他0 | 跌破下轨40；距下轨1.5%以内25；跌破中轨10；其他0 |
| RSI，满分35 | <32得35；32–<38得25；38–<43得15；其他0 | <30得35；30–<35得25；35–<40得10；其他0 |
| MACD，满分25 | 金叉25；负柱连续两天缩短20；负柱缩短一天10；其他0 | 同左 |

“跌破”使用严格小于，价格等于下轨归入“接近下轨”档。金叉包含前一天 DIF=DEA、当天 DIF>DEA 的情况。
负柱连续两天缩短需满足三根负柱 h[t-2]<h[t-1]<h[t]<0；金叉优先于缩柱分。
采用复权日线，BOLL(20,2)、RSI(14)、MACD(12,26,9)。手动运行时剔除纽约16:00前的当日日线。

## 邮件

新版 HTML 邮件包括：扫描概览、按总分排序的标的卡片、三项得分条与评分理由、复权收盘价和关键指标、AI 三段研判，以及全部已成功评分标的的得分一览。
兼容手机宽度，使用内联样式和表格，不加载远程图片；同时附带纯文本备份。
不足70分时只发状态和评分一览；AI 失败时仍发技术评分并注明研判不可用。行情读取失败会在邮件中明确标注，不伪装成“无达标信号”，同时让 Actions 报错。

## GitHub 部署

仓库默认分支需包含 stock_ai_scanner.py、email_reports.py、stock-requirements.txt、tests/ 和 .github/workflows/daily_scan.yml。
在 Settings → Secrets and variables → Actions 中配置三个 Secrets：

- AI_API_KEY：你的 DeepSeek API Key（仅达标研判需要；空值不影响无达标状态邮件）
- QQ_EMAIL：你的 QQ 邮箱地址
- QQ_AUTH_CODE：QQ 邮箱 SMTP 授权码，不能填 QQ 密码

默认接口 https://api.deepseek.com，模型 deepseek-flash。其他兼容接口可另设 AI_BASE_URL、AI_MODEL。
工作流在 UTC 周日至周四22:30触发，即北京时间周一至周五06:30。
GitHub定时任务可能延迟；公开仓库60天无活动时可能被自动停用。
当前公开仓库的标准 GitHub 托管运行器免费。无达标邮件不调用模型，不产生 DeepSeek API 费用；达标时的模型研判按 DeepSeek 实际 token 用量计费。QQ 邮件使用现有邮箱 SMTP 服务。

Actions → Daily US stock AI scan → Run workflow：不勾选 test_email 运行真实扫描；勾选则发送标注“虚构示例”的版式测试邮件，不调用行情和AI。

## 本地运行

~~~powershell
python -m venv .venv-stock
.\\.venv-stock\\Scripts\\python.exe -m pip install -r stock-requirements.txt
$env:AI_API_KEY = "你的DeepSeek API Key"
$env:QQ_EMAIL = "你的QQ邮箱地址"
$env:QQ_AUTH_CODE = "你的SMTP授权码"
.\\.venv-stock\\Scripts\\python.exe stock_ai_scanner.py
~~~

离线生成邮件预览：python stock_ai_scanner.py --preview-email outputs/email-preview.html。
运行验证：python -m unittest discover -s tests -v。
