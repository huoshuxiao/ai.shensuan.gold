# ai.shensuan.gold

目标：跟踪上海期货交易所黄金au指数、纽约COMEX黄金等，黄金ETF量化研調與投資決策系統，给出交易建议。别人贪婪时恐惧，别人恐惧时贪婪。缺乏金融领域专业知识，人工审查逻辑有困难。读 **Links** 中链接的网站系统设计，生成AGENTS.md文件，输出系统设计。

## 技术栈

### 必选

- 机器配置，ubuntu 20.04,机器配置16G内存，2G显存，i7-6700HQ CPU
- python path: /usr/bin/python3.10

### 可选选

- docker
- ~/miniconda3/
- rdagent 本地llm(qwen3.5:9b/qwen3.5:4b/qwen2.5:7b/deepseek-r1:8b 可以用时间换结论，选推理和生成代码质量最优的模型)

## 需求

### 功能需求

- 给出实现流程，逐步实现
- 手动交易。交易数据手动录入系统
- 使用敏捷用台账md文件管理任务情况，有TODO/Process/Done/ISSUE/PENDING/RISK等
- 任务DONE时，自动git commit，不 push
- 程序运行链路图，要有节点状态，比如LLM或数据超时等，通过链路能展示节点状态，不需要通过日志才发现断路情况
- 数据质量必须高，必须保证数据的一致性，连续性
- “别人贪婪时恐惧，别人恐惧时贪婪”：必须用两个或更多独立指数/指标的高低点交叉、重叠等组合信号做统计分析，给出买入低点、卖出高点的命中率，以及盈利和亏损的概率；所有结论必须带样本量、时间区间、成本后结果与置信区间，用数据说话，禁止用故事代替数据。

### 非功能需求

- 给出适用opencode和qoder的规则文件
- 依赖通过requirements.txt安装，不使用命令安装。源下载失败，改用国内源
- 代码必须有测试用例
- 日志保留7天
- 非正式代码先在test目录下进行验证，验证成功后移入main目录
- 生成的代码及配置文件要有详细的注释便于项目维护。__init__.py 除外
- 测试代码文件也分目录

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
