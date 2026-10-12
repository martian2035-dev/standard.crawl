# 标准信息采集与核验

面向公开标准信息的数据采集和有效性核验工具，支持国家标准、行业标准和地方标准。

## 功能

- 按标准号维护标准列表，补充新增记录并更新状态、日期等字段。
- 支持分页采集、断点续跑和请求限速。
- 按完整标准号查询官方来源，核实状态及明确记载的替代关系。
- 保留来源链接和核验时间；查询失败或证据冲突时标记待核查。
- 支持 GitHub Actions 月度更新。

## 数据来源

| 来源 | 用途 |
| --- | --- |
| [全国标准信息公共服务平台](https://std.samr.gov.cn/) | 标准检索、详情与替代关系 |
| [国家标准全文公开系统](https://openstd.samr.gov.cn/bzgk/std/) | 国家标准列表与状态 |
| [行业标准备案平台](https://hbba.sacinfo.org.cn/) | 行业标准信息 |
| [地方标准备案平台](https://dbba.sacinfo.org.cn/) | 地方标准信息 |

## 使用

安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-audit.txt
```

查看采集状态：

```bash
.venv/bin/python standard_crawl.py --status
```

检查公开查询清单：

```bash
.venv/bin/python standard_audit.py 公开标准查询清单.csv --dry-run
```

详细用法见 [本地核验](docs/统计表本地核验.md) 和 [GitHub 月度任务](docs/GitHub迁移与月度任务.md)。

## 查询清单与原表

公开查询清单仅含标准类型和标准号，已去重并排序。原始 Excel、文件名、文件大小、备注及作者元数据保留在本地，不随公开版本分发。

核验结果以官方网页证据为依据。“未查到”不等同于“废止”；尚未覆盖的标准或不明确的替代关系需要人工核对。本站不提供标准全文下载。

## 测试

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```
