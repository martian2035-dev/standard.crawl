# GitHub 月度任务

默认分支使用 `main`。两条任务独立运行，支持手动启动和分片续跑。

## 公开输入

仓库中的 `公开标准查询清单.csv` 只包含 `标准类型` 和 `标准号`，去重后按顺序排列。原始 Excel 留在本地，不提交到公开仓库。

在本地更新清单：

```bash
.venv/bin/python scripts/public_input.py prepare ./标准信息统计表.xlsx
```

公开清单不含原始行号、文件重复数量、文件名、文件大小、备注、原表链接或作者属性。云端报告、缓存和附件均以公开清单为输入。

## 首次发布

使用导出脚本建立新的公开工作目录。导出采用文件白名单，不携带旧 Git 历史、原始 Excel、凭证、本地配置、数据库或运行缓存。

```bash
python3 scripts/export_github.py ../standard_crawl_public
cd ../standard_crawl_public

git init -b main
git add .
git commit -m "初始化标准信息采集与核验项目"

gh auth login --hostname github.com --web --git-protocol https --scopes workflow
gh auth setup-git
gh repo create standard-crawl --public --source=. --remote=origin --push
```

目标目录须尚不存在，仓库名称可以自行修改。后续在这个新目录维护公开项目。旧 Git 历史不会因当前文件清理而消失，因此不要使用 `git push --mirror` 或推送旧分支、标签；首次发布只推新目录的 `main`。

## 权限与运行安排

在仓库 **Settings → Actions → General** 中允许 Actions，并将 **Workflow permissions** 设为 **Read and write permissions**。如果为 `main` 启用了分支保护或 ruleset，需要允许数据回写。工作流使用仓库自带的 `GITHUB_TOKEN`，无需配置个人访问令牌。

| 工作流 | 北京时间 | 用途 |
| --- | --- | --- |
| 每月更新标准列表与数据库 | 每月 2 日 03:43 | 扫描国家、行业、地方标准列表，按标准号新增或更新 |
| 每月联网核验统计表 | 每月 5 日 04:17 | 对公开清单逐项核实状态、日期及替代版本 |
| 离线测试 | 代码推送或 PR | 验证采集、核验和隐私边界 |

cron 已从北京时间换算为 UTC。定时任务可能延迟；公开仓库长期没有活动时，定时任务可能需要重新启用。[GitHub 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

首次可手动启动：

```bash
gh workflow run monthly-crawl.yml --ref main
gh workflow run monthly-audit.yml --ref main
gh run list --limit 10
```

每片预算为 60 分钟，未完成时自动启动下一片。两条任务均最多累计 100 片；无进展、输入不一致或站点受限时暂停，查看 Actions 摘要再恢复。不要为同一工作流同时启动不同月份的任务链。

## 数据更新与核验范围

列表每月从第一页重扫，保留历史记录，按标准号新增和更新。旧标准可能在本月才被废止，因此不只按发布日期截断请求。来源不再返回的历史条目不会仅因消失而被认定为废止。

默认月度采集更新国家、行业、地方三份 CSV。已有工程建设标准 CSV 可参与数据库构建，其更新仍需单独运行 `--only engineering`。

核验开始时使用当时的 CSV 建立固定历史基线；受支持的标准即使已在基线中命中，也需要官网查询。国际标准及未支持的编号格式标为未覆盖。全表已处理表示已尝试所有受支持条目，不表示每条状态都已确认。

## 查看结果

- 三份标准列表直接在仓库根目录更新。
- 月度数据库位于 `standards-YYYY-MM` Release，包含压缩数据库与哈希清单。大数据库不进入 Git 历史。
- 公开清单的核验结果位于 `reports/`，逐月归档和网页证据位于 `audit-YYYY-MM` Release。
- 本地原表的补充方法见 [本地核验](统计表本地核验.md#公开清单与本地补充)。下载报告后按标准号映射回本地原表，保留重复行及私有字段。

## 中断恢复

采集进度保存在 `.github_crawl_state/`。手动启动并填写相同的 `campaign`（格式 `YYYY-MM`）即可继续；失败页不会被跳过，完整采集完成前不发布当月数据库。

核验检查点保存在每片运行的 `audit-checkpoint` 附件中，保留 7 天。恢复时在 Actions 的 Run workflow 表单填写相同月份，以及最近一次有完整附件的运行 ID（`restore_run_id`）。续片校验公开清单和基线哈希，附件缺失或清单变化时停止。

附件过期后，不填 `restore_run_id`，重新开始。每月新任务使用新缓存，同月续片才复用已有查询。403、429 或验证码会触发站点暂停，应等待冷却再恢复。
