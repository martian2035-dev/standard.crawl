# GitHub 公开仓库与月度任务

本项目支持在 GitHub Actions 上完成月度标准数据更新和统计表联网核验。默认分支使用 `main`。
原有 CNB 配置保留，GitHub 流程使用独立的进度目录，不调用 CNB 或 VPS 部署。

## 运行安排与产物

| 工作流 | 北京时间 | 行为 | 产物 |
| --- | --- | --- | --- |
| 每月更新标准列表与数据库 | 每月 2 日 03:43 | 重扫官方列表，按标准号新增和更新；每片最多采集 60 分钟 | 三份 CSV 与游标回写 `main`；数据库发布到 `standards-YYYY-MM` Release |
| 每月联网核验统计表 | 每月 5 日 04:17 | 针对根目录原始 Excel 逐项检索官网，保存网页证据；每片最多 100 个标准、600 次请求、60 分钟 | 补充版 Excel、核验 CSV、摘要回写 `reports/`；详细证据发布到 `audit-YYYY-MM` Release |
| 离线测试 | 修改代码并推送或提出 PR | 运行全部测试，不访问标准网站 | Actions 测试结果 |

GitHub 的 cron 使用 UTC；代码中已经换算北京时间，并避开整点。定时任务可能延迟，默认分支必须包含工作流。公开仓库连续 60 天没有活动时，GitHub 可能停用定时任务，需要在 Actions 页面重新启用。[GitHub 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

两条月度任务独立运行。核验在启动时用当时仓库内的 CSV 建立固定历史基线，随后每个受支持的标准都联网核验，即使在历史库中命中也不会直接认定为有效。采集任务异常不会让核验流程把历史状态冒充最新官网状态。

## 数据源与“增量更新”的范围

- 国家标准列表来自[国家标准全文公开系统](https://openstd.samr.gov.cn/bzgk/std/)。
- 行业、地方标准分别来自[行业标准备案平台](https://hbba.sacinfo.org.cn/)与[地方标准备案平台](https://dbba.sacinfo.org.cn/)，对应[全国标准信息公共服务平台](https://std.samr.gov.cn/)的行业、地方标准来源。
- 统计表核验按标准号在全国标准信息公共服务平台、国家标准全文公开系统及行业、地方备案平台定向检索；不是只查本地 CSV，也不依赖搜索引擎摘要判定有效性。

这里的增量是**保留历史数据，新增标准号，更新同号记录中变化的状态等字段**。网络端每月仍从第一页重扫，因为旧标准可能本月才被废止；只按发布日期截断会漏掉这类变化。数据源不再返回的历史条目保留，不凭“列表消失”推断废止。项目没有官方变更日志接口，因此不承诺只请求上月新增记录就能得到完整状态变更。

GitHub 默认更新三份文件：

```text
国家公开标准信息.csv
行业公开标准信息.csv
地方公开标准信息.csv
```

已有 `工程建设标准信息.csv` 若保留在仓库，数据库构建仍会收录；该文件不在本次 GitHub 月度采集范围内。需要更新时可运行现有采集器的 `--only engineering` 任务。

## 首次迁移到 GitHub

### 1. 导出新仓库，避免把旧大文件历史推上去

旧 Git 历史中包含超过 100 MiB 的 `standard_db/standards.db`。GitHub 会拒收，即使最新提交已删除该文件，直接推旧历史也不能解决。[GitHub 大文件限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

使用提供的导出脚本复制**当前工作树**（包括本次尚未提交的代码修改、三份 CSV 和原始 Excel），在一个新目录建立 GitHub 仓库。脚本不会重写 CNB 历史，不复制 `.git`、旧数据库、旧采集游标、本地核验缓存和历史报告。

在当前项目目录执行：

```bash
python3 scripts/export_github.py ../standard_crawl_github
cd ../standard_crawl_github
git init -b main
git add .
git commit -m "初始化 GitHub 标准采集与月度核验项目"
```

导出目录必须尚不存在。首次迁移后，以新目录作为 GitHub 项目的日常工作目录；原目录仍对应 CNB。

### 2. 登录并创建公开仓库

```bash
gh auth login --hostname github.com --web --git-protocol https --scopes workflow
gh auth setup-git
gh repo create standard-crawl --public --source=. --remote=origin --push
```

`standard-crawl` 是建议名称，可以替换为你希望的名称。该命令在当前登录用户下建立**公开仓库**，推送的原始 Excel、CSV、报告及 Release 都可被公开访问。

如果不使用 `gh`，可在 GitHub 网页创建空的 Public 仓库（不要预先添加 README），再按网页给出的远程地址执行 `git remote add origin ...` 和 `git push -u origin main`。

### 3. 设置 Actions 权限

进入仓库 **Settings → Actions → General**：

1. 允许仓库运行 GitHub Actions，并允许使用官方 `actions/*`。
2. **Workflow permissions** 选择 **Read and write permissions**，保存。
3. 如果为 `main` 设置了禁止直接写入的分支保护或 ruleset，需要允许此数据回写流程写入；否则回写会失败。组织级权限限制也需要管理员允许。

工作流声明 `contents: write`（提交数据、发布 Release）与 `actions: write`（自动启动下一片），使用 GitHub 内置 `GITHUB_TOKEN`。无需配置 PAT、CNB_TOKEN、SSH 密钥或搜索 API 密钥。GitHub 明确允许内置令牌触发 `workflow_dispatch`。[GITHUB_TOKEN 行为](https://docs.github.com/en/actions/concepts/security/github_token)

### 4. 第一次手动启动

在新仓库目录执行：

```bash
# 不等到下个月，现在启动；月份自动按北京时间计算。
gh workflow run monthly-crawl.yml --ref main
gh workflow run monthly-audit.yml --ref main

# 查看近期运行；也可到网页 Actions 查看日志和摘要。
gh run list --limit 10
```

核验可以独立运行；希望使用本轮采集完成后的历史基线时，等采集 Release 发布后再启动核验。每条工作流会自动连续分片，无需逐片点击。每条工作流最多累计 100 片；失败、无进展或达到上限会暂停并报错。不要同时为同一工作流启动多条不同月份的链。

如果官网限制 GitHub 托管 runner 的 IP，日志会记录请求失败并暂停。先确认官网恢复可访问，再恢复任务；必要时修改 `runs-on` 使用你已配置、能够访问官网的自托管 runner，保留现有限速设置。

## 获取数据库和核验结果

### 数据库

采集全部完成且数据库完整性和哈希校验通过后，Release `standards-YYYY-MM` 包含：

```text
standards.db.gz
manifest.json
国家公开标准信息.csv
行业公开标准信息.csv
地方公开标准信息.csv
```

例如下载 2026 年 10 月成果：

```bash
mkdir -p standard_db
gh release download standards-2026-10 --pattern standards.db.gz --pattern manifest.json --dir standard_db --clobber
gzip -df standard_db/standards.db.gz
python build_db.py --out standard_db --verify
```

`manifest.json` 的 `sha256` 校验的是**解压后的 SQLite 文件**，同时记录三份 CSV 的哈希。大数据库不进入 GitHub 的 Git 历史，后续每月也不会再次触发 100 MiB 限制。

### 补充版 Excel

最新结果在仓库的 `reports/标准信息统计表_已核验.xlsx`；按月归档位于 `audit-YYYY-MM` Release。

- 根目录 `标准信息统计表.xlsx` 是每月核验的稳定输入，不被自动覆盖。
- 补充表保留原工作表、行顺序、重复行、原字段值、公式和样式，并在右侧追加核验名称、状态、发布日期、实施日期、废止日期、现行版本、对应日期、网页来源、时间和结论。
- 原表旧状态仍保留，最新结论查看追加的 `核验状态`、`核验现行版本` 等列。
- 缺失信息只在获取证据后补入核验列；网络失败、来源冲突、替代关系不明确时保留待核查或人工复核。
- 国际标准或解析器尚不覆盖的编号会明确标为“未覆盖”。“全表已处理”表示所有受支持条目已尝试，不代表每条均已成功确认。
- `reports/标准信息核验结果.csv` 提供完整字段和证据索引；Release 中 `核验证据.tar.gz` 包含逐项 JSONL、网页快照、历史基线和运行摘要。

修改待核验清单时，更新根目录原始 Excel 并提交。不要用已经追加核验列的输出表覆盖输入表，以免新旧月份结果混用。整个月的新任务使用新缓存，同月续片才复用已核验记录。

## 失败后恢复

采集每片均回写 CSV 与 `.github_crawl_state/`，同一月份手动启动即从失败页或上次分片位置继续：

```bash
gh workflow run monthly-crawl.yml --ref main -f campaign=2026-10
```

单页重试失败后不跳页，不发布当月数据库；已成功处理的页面仍会保存。若需要对同一个月份完全重扫，在新仓库删除 `.github_crawl_state/` 并提交后再启动，历史 CSV 保留。

核验状态保存在每片 Actions 的 `audit-checkpoint` 附件中，保留 7 天。找到最近一次有完整附件的运行 ID，例如 `123456789`，然后：

```bash
gh workflow run monthly-audit.yml --ref main -f campaign=2026-10 -f restore_run_id=123456789
```

续片检查月份、原表 SHA256、查询清单及历史基线；附件缺失或原表变化时停止，不悄悄丢弃进度。附件过期后不填 `restore_run_id`，重新开始全表核验。已发布的 Release 不受这 7 天限制。

403、429、验证码或连续请求失败会触发站点暂停；应等待冷却再恢复，不要频繁重跑。若作业被强制取消或上传失败，使用更早的完整附件恢复。

## 本地验证与调试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-audit.txt pytest
.venv/bin/python -m pytest -q

# 只用已有 CSV 构建 Release 数据库，不访问网络。
.venv/bin/python scripts/github_monthly.py database --campaign 2026-10

# 小预算运行核验，结果先留在被忽略的 .github_audit_work/。
.venv/bin/python scripts/github_monthly.py audit --campaign 2026-10 --budget-seconds 60
# 同目录重复执行即可延续；重新开始前先把旧检查点目录移到别处。
```

Actions 使用 Python 3.12；脚本依赖通过 requirements 安装，与原项目的 uv/Python 3.14 环境兼容。
