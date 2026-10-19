# ai.shensuan.gold

目标：跟踪上海期货交易所黄金au指数、纽约COMEX黄金等，黄金ETF量化研調與投資決策系統。别人贪婪时恐惧，别人恐惧时贪婪。缺乏金融领域专业知识，人工审查逻辑有困难。读 **Links** 中链接的网站系统设计，生成AGENTS.md文件，输出系统设计。

## 技术栈

 1. python path: /usr/bin/python3.10
 2. 依赖通过requirements.txt安装，不使用命令安装。源下载失败，改用国内源
 3. 给出实现流程，逐步实现
 4. 给出适用opencode和qoder的规则
 5. 使用敏捷用台账md文件管理任务情况，有TODO/Process/Done/ISSUE/PANDING/RISK等
 6. 任务DONE时，自动git commit
 7. ~/miniconda3/
 8. docker
 9. 代码必须有测试用例
 10. 机器配置，ubuntu 20.04,机器配置16G内存，2G显存，i7-6700HQ CPU
 11. rdagent 本地llm(qwen3.5:4b或qwen3.5:9b或deepseek-r1:8b 可以用时间换结论)
 12. 手动交易。交易数据手动录入系统

## Links

- [项目结构](PROJECT.md)
- [AI Hedge Fund](https://github.com/virattt/ai-hedge-fund/tree/main)
- [AI 多 Agent 量化投资系统](https://github.com/2464404039/a-share-agents)
- [AI Hedge Fund API (v2.0)](https://github.com/tbdavid2019/ai-hedge-fund-API)
- [Alpha Desk](https://github.com/JingHao-Leon/dsh-alpha-desk)
- [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN/tree/v3.0)
- [TradingAgents-Astock](https://github.com/simonlin1212/TradingAgents-astock)
- [aiagents-stock](https://github.com/oficcejo/aiagents-stock)
