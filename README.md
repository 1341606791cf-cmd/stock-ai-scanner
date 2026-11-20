# 美股本地评分报告

监控 QQQ、GOOGL、NVDA、AAPL、MSFT、ASML、COST。默认运行只生成并打开本地HTML报告，不发送QQ邮件、不调用AI，也不需要邮箱或大模型密钥。

## 本地查看与更新

双击 outputs/刷新评分报告.cmd，导入GitHub最新公开行情快照并打开 outputs/stock-report-latest.html。页面会显示行情获取时间及纽约交易日；旧快照不会冒充最新行情。直接刷新浏览器只会重读HTML，重新导入须运行上述命令。

也可运行：

~~~powershell
.\.venv-stock\Scripts\python.exe local_stock_report.py
~~~

默认从GitHub公开文件 reports/latest-data.json 导入。原始行情来自Yahoo Finance/yfinance、3个月复权日线。GitHub云端工作流 Local stock report data 可在Actions中手动 Run workflow 更新快照，或按原北京时间周一至周五06:30计划更新。GitHub定时触发可能延迟或漏触发，页面时间戳是判断新旧的依据。

本机网络可访问Yahoo时，可直接重新下载，不等待云端：

~~~powershell
.\.venv-stock\Scripts\python.exe local_stock_report.py --source yahoo
~~~

离线导入已保存的数据：

~~~powershell
.\.venv-stock\Scripts\python.exe local_stock_report.py --input-json outputs/stock-report-latest.json --no-open
~~~

## 两种评分视图

- 已收盘评分：纽约16:00之前排除当日未完成日线，使用最近完整日线。
- 盘中试算：如果Yahoo返回当日未收盘日线，则单独计算并标记“未确认”；价格可能延迟，分数会变化。
- 每只标的可展开查看BOLL、RSI、MACD数值、命中档位、分项加总及最近三根指标。
- 可搜索代码或筛选≥70分，并下载本次原始OHLCV JSON。
- 行情失败会明确注明；没有可用完整日线时不会覆盖最后一份正常报告。

## 原评分规则

每个维度只取最高命中档，三个维度相加，不重复累计。70分表示规则匹配程度，不代表胜率或自动买入指令。

| 维度 | QQQ指数档 | 其他个股档 |
|---|---|---|
| BOLL /40 | 跌破下轨40；距下轨1%以内30；跌破中轨15；其他0 | 跌破下轨40；距下轨1.5%以内25；跌破中轨10；其他0 |
| RSI /35 | <32得35；32–<38得25；38–<43得15；其他0 | <30得35；30–<35得25；35–<40得10；其他0 |
| MACD /25 | 当日金叉25；负柱连续两天缩短20；负柱缩短一天10；其他0 | 同左 |

BOLL(20,2)使用样本标准差；RSI(14)使用alpha=1/14的EW平滑；MACD(12,26,9)柱值为2×(DIF−DEA)。三个月窗口、数据复权和初始化方式可能造成不同平台之间的数值差异。

## 云端与依赖

GitHub工作流只安装依赖、验证、更新公开行情JSON和HTML，不读取QQ或AI Secrets。原邮箱发送步骤已移除；历史邮件辅助函数仅保留作兼容，不是默认执行入口。

首次部署或主动提交带 [refresh-report] 的变更会构建一次快照，普通代码提交只运行测试。正式使用建议从Actions手动刷新或核对计划生成时间；不会在本机后台自动发送任何通知。

依赖安装：python -m pip install -r stock-requirements.txt
测试：python -m unittest discover -s tests -v

