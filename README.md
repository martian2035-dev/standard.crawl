# standard_crawl —— 标准信息采集与构建流水线

抓取国家标准 / 行业标准 / 地方标准 / 工程建设标准，汇总构建为 SQLite 数据库 **standard_db**，
支持在 GitHub Actions 或 CNB 云原生构建上每月自动运行。

## GitHub 使用入口

迁移、创建公开仓库、启用权限和首次运行，见 **[GitHub 迁移与月度任务](docs/GitHub迁移与月度任务.md)**。

- 每月 2 日 03:43（北京时间）：扫描国家、行业、地方标准列表，按标准号新增或更新三份 CSV；构建并校验数据库后发布到 GitHub Release。
- 每月 5 日 04:17（北京时间）：对根目录 `标准信息统计表.xlsx` 重新联网核验，生成 `reports/标准信息统计表_已核验.xlsx`，保留原工作表及原值并补充核验列；网页证据按月发布。
- 两条任务均自动分片续跑，失败时保留检查点；只需 GitHub 内置令牌。
- 历史数据库超过 GitHub 普通 Git 单文件限制，请按迁移说明导出新仓库，不能直接把当前旧历史推到 GitHub。

以下为既有 CNB 管道、本地使用及数据库结构说明；其中“数据库入库”和 CNB 部署步骤只适用于原 CNB 流程。GitHub 流程以以上新文档为准。

```
┌──────────────────── ① 数据采集管道 (crawl) ─────────────────────┐
│                                                                 │
│  每月 1 号 03:00 (Asia/Shanghai) ──► 开启「采集行动」             │
│                                                                 │
│   ┌── 分片 #1 (≤70min) ─┐   ┌── 分片 #2 (≤70min) ─┐   ┌── ... ──┐ │
│   │ 抓 N 页 → 落盘      │   │ 续爬 → 落盘         │   │         │ │
│   │ 回写 CSV+游标       │──►│ 回写 CSV+游标       │──►│         │ │
│   └────────────────────┘   └────────────────────┘   └─────────┘ │
│                                                       │         │
│  产出：3 个 CSV + .crawl_state/（页面游标）            │         │
└───────────────────────────────────────────────────────┼─────────┘
                                                        │ 全部抓完
                                                        ▼
┌──────────────────── ② 构建管道 (build) ─────────────────────────┐
│  只消费已产出的 CSV，不碰网络                                    │
│  build_db.py ──► standard_db/standards.db + manifest.json       │
│                        │                                        │
│                        └─► 归档到本仓库（只保留在代码库内）        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 一、为什么要这么设计

### 1. 流水线有 120 分钟硬上限，抓取要 4~5 小时

实测报错：

```
The maximum pipeline duration is exceeded: 120min
Finished, code: 137, duration: 1h 59m
```

这个上限是**平台侧的，`timeout` 配置改不动**（[CNB 超时策略](https://docs.cnb.cool/zh/build/timeout.html)
文档写流水线 20 小时，但本环境实际按 120 分钟执行）。而全量抓取约 6100 页、
每页 2~3 秒，需要 **4~5 小时**。

所以采集被设计成**时间盒分片**：

| 机制 | 说明 |
| --- | --- |
| 时间预算 | 每片 70 分钟（`CRAWL_BUDGET_SECONDS`），到点主动优雅退出 |
| 页面游标 | 每个任务的进度写入 `.crawl_state/state.json` |
| 定期落盘 | 每 25 页把 CSV 原子写盘（临时文件 + `os.replace`），容器被 `SIGKILL` 最多丢 25 页 |
| 进度回写 | 每片结束把 CSV + 游标 `git push` 回仓库 |
| 自动续跑 | 未抓完 → `cnb:apply` 触发下一片；抓完 → 触发构建管道 |
| 卡死保护 | 同一页连续失败 3 次就记录并跳过，**保证每片必须前进**，避免链式调用原地打转；最大 24 片 |

> 为什么每月都从第 1 页重扫？列表按发布日期倒序，新增标准会插到最前面，
> 只从断点往后扫会漏掉新条目。重复数据靠「标准号」去重，不会重复入库；
> 已有标准再次抓到且内容有变化（如状态由「现行」变为「废止」）时，更新 CSV 中对应的行。

### 2. 采集与构建解耦

| | 采集管道 | 构建管道 |
| --- | --- | --- |
| 事件 | `crontab`、`api_trigger_crawl`、`api_trigger_crawl_continue` | `api_trigger_build`、`push`（仅代码变更） |
| 职责 | 只抓数据，产出 CSV + 游标 | 只消费 CSV，产出 standard_db |
| 网络 | 依赖上游站点 | **完全不访问外网** |
| 失败影响 | 抓取失败不影响已构建的数据库 | 构建失败不影响已抓到的数据 |

构建管道可以独立重跑：抓到一半也可以先构建一份可用的数据库。

---

## 二、目录结构

| 路径 | 说明 |
| --- | --- |
| `standard_crawl.py` | **采集器**：断点续爬 + 进度持久化 + 时间盒分片 |
| `standard_audit.py` | **本地统计表核验**：逐标准查询、正向替代链与字段来源证据 |
| `audit_pipeline.py` | **独立核验流水线**：先匹配历史库、分片联网补缺、下载结果合并原表 |
| `audit_inputs/` | 最小标准查询清单与哈希映射，不含原始文件信息 |
| `local_audit/` | **本地核验产物**：CSV、JSONL、SQLite 与网页快照，被 Git 忽略 |
| `build_db.py` | **构建器**：3 个 CSV → `standard_db/standards.db`，带 `--verify` |
| `.cnb.yml` | 流水线定义：采集管道 + 构建管道 |
| `.crawl_state/` | **采集进度**（页面游标、统计、跳过页），入库 |
| `standard_db/` | **构建产物**（`standards.db` + `manifest.json`），入库 |
| `scripts/crawl_chunk.sh` | 跑一个采集分片 |
| `scripts/sync_latest.sh` | 流水线启动时同步到远端最新提交 |
| `scripts/commit_back.sh` | 回写 CSV + 采集进度 |
| `scripts/publish_db.sh` | 归档 standard_db 到本仓库 |
| `scripts/run_local.sh` | 本地一键运行 |
| `scripts/git-askpass.sh` | Git 凭证辅助，令牌不进 URL/日志 |
| `import_standards.py` | 旧脚本（从 xlsx 导入外部项目库），保留未动 |

---

## 三、本地使用

按 Excel 统计表逐项核验标准状态、废止后的现行版本及发布日期，并记录逐字段网页来源：

```bash
uv pip install --python .venv/bin/python -r requirements-local.txt
.venv/bin/python standard_audit.py /Users/northwang/Downloads/standard_code/标准信息统计表.xlsx
```

该功能独立运行，结果及网页证据仅保存至被 Git 忽略的 `local_audit/`，不会触发 CNB 或 VPS 推送。
支持小批量试采、缓存、失败重试和官方来源交叉核验，详见 [统计表本地核验说明](docs/统计表本地核验.md)。

统计表的 CNB 独立核验先使用已有历史库筛查，再自动连续分片处理全表。独立分支 `codex/standard-table-audit` 推送一次即可启动；本地运行 `bash scripts/pull_audit.sh` 自动下载、合并原表并生成 `local_audit/verified/标准信息核验结果.xlsx`。进度自动恢复，不需要手动填写 SHA。成果附件保留 7 天，本地结果不回写 Git 或部署到 VPS。详见 [CNB 独立核验流水线](docs/CNB独立核验流水线.md)。

```bash
# 首次
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 抓 1 个分片 → 构建 → 校验
bash scripts/run_local.sh

# 本地循环跑分片直到抓完，再构建（会跑几个小时）
bash scripts/run_local.sh --crawl-until-done

# 只看采集进度
bash scripts/run_local.sh --status

# 跳过抓取，只用现有 CSV 重建数据库（几秒）
bash scripts/run_local.sh --no-crawl

# 开启新的采集行动（游标归零）
bash scripts/run_local.sh --new-campaign
```

快速试跑（每片只抓 5 页）：

```bash
CRAWL_MAX_PAGES=5 CRAWL_BUDGET_SECONDS=120 bash scripts/run_local.sh --crawl-until-done
```

单独调用采集器：

```bash
python standard_crawl.py --status
python standard_crawl.py --only industry --max-pages 10 --budget-seconds 60
python standard_crawl.py --new-campaign --budget-seconds 4200
python standard_crawl.py --emit-next          # 输出 ##[set-output crawl_next=...]
```

采集器参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--budget-seconds` | 4500 | 本片时间预算（秒） |
| `--checkpoint-every` | 25 | 每多少页落盘一次 |
| `--page-size` | 50 | 每页条数 |
| `--delay` | 0.4 | 每页间隔秒数 |
| `--page-retries` | 3 | 单页连续失败几次后跳过（保证前进） |
| `--max-pages` | 0 | 本片最多抓多少页（0=不限，调试用） |
| `--max-chunks` | 24 | 一次采集行动最多分片数 |
| `--only` | 全部 | 只采集指定任务，可重复 |
| `--new-campaign` | — | 游标归零，开启新采集行动 |
| `--status` / `--emit-next` | — | 只读命令 |

退出码：`0` 本片正常结束；`1` 本片零进展 / 超过最大分片数 → 流水线报错并停止链式调用。

---

## 四、CNB 配置步骤

### 第 1 步：确认默认分支是 `main`

`.cnb.yml` 绑定 `main`（定时任务不支持 glob，必须写明确的单一分支名）。
若默认分支是 `master`，把 `.cnb.yml` 顶层的 `main:` 改成 `master:`，
并同步修改 `scripts/*.sh` 里 `BRANCH` 的默认值。

### 第 2 步：确认仓库设置允许自动触发

仓库 **设置 → 流水线**，确认勾选了「允许自动触发」，
否则 `push` 不会触发流水线。

### 第 3 步：推送代码

```bash
git add -A
git commit -m "feat(ci): 采集与构建解耦 + 断点续爬"
git push -u origin main
```

推送后 `push` 事件会触发一次构建校验（`ifModify` 命中 `.cnb.yml`/`scripts/**`）。

### 第 4 步：确认定时任务已登记

仓库 → **流水线 / 定时任务**，应出现：

| 名称 | 计划 | 分支 |
| --- | --- | --- |
| 每月启动标准数据采集 | `0 3 1 * *` | main |

要点：

- **时区 Asia/Shanghai**，`0 3 1 * *` = 每月 1 号凌晨 3:00（北京时间）。
- **最小间隔 5 分钟**。
- **执行身份 = 最后修改并推送这段 `crontab` 配置的人**；这个人被移出仓库会导致任务失败。
- 定时任务只在推送 `.cnb.yml` 所在分支时同步登记/删除。

### 第 5 步：手动验证一次完整链路（强烈建议）

**改配置的地方只有一个：仓库根目录的 `.cnb.yml`**（不是 CNB 网页上的表单）。
所有 `env:` 都是这个文件里的字段，按 `流水线 → 阶段` 层级缩进：

| 位置 | 行号 | 作用 |
| --- | --- | --- |
| 采集管道的 `env` | `.cnb.yml` 第 54~63 行 | `CRAWL_BUDGET_SECONDS` / `CRAWL_CHECKPOINT_EVERY` / `CRAWL_DELAY` 等 |
| 构建管道的 `env` | `.cnb.yml` 第 182 行起 | `DB_COMPRESS` 等 |
| 冒烟测试的 `env` | `.cnb.yml` 的 `api_trigger_crawl_smoke` | 一般不用改 |

在 CNB 网页上可以直接编辑：仓库 → 文件 → `.cnb.yml` → 右上角编辑 → 提交。
（编辑 `.cnb.yml` 需要提交到 `main`，提交后配置才会生效。）

#### 5.1 先跑冒烟测试（约 2 分钟，不改动仓库数据）

仓库 → **流水线** → **触发流水线 / 手动触发** → 选择：

| 项 | 值 |
| --- | --- |
| 分支 | `main` |
| 事件 | `api_trigger_crawl_smoke` |

它会自动完成：同步代码 → 装依赖 → 抓 3 页 → 从游标续抓 2 页 →
自检「CSV 无重复、游标正确续接到第 5 页」→ 打印进度与下一步动作。

全程在 `/tmp/smoke` 里跑，**不碰仓库里的 CSV 和 `.crawl_state`**，
可以放心反复执行。

#### 5.2 再跑真实采集（这一步是你要做的工作，不会白跑）

冒烟通过后，同样方式触发事件 **`api_trigger_crawl`**（首片，游标归零）。
它会：

1. 同步 → 装依赖 → 抓一个分片（最多 70 分钟）；
2. 回写 CSV 与 `.crawl_state/` 到仓库；
3. 自动判断：没抓完 → 触发 `api_trigger_crawl_continue` 继续下一片；
   抓完 → 触发 `api_trigger_build` 构建并归档 `standard_db`。

因为每一步都有游标落盘，**中途失败不丢数据**，重跑会从断点续上。

如果你想先小范围试一次完整链路（包括回写和链式调用），可以在网页上临时编辑
`.cnb.yml`，在采集管道的 `env`（第 54~63 行）里加两行：

```yaml
        CRAWL_MAX_PAGES: "5"      # 临时：每片只抓 5 页
        CRAWL_MAX_CHUNKS: "2"     # 临时：最多链 2 片
```

这样第一片抓 5 页 → 回写 → 自动触发 `api_trigger_crawl_continue` → 第二片再抓 5 页
→ 到达分片上限后主动停止。**最后这条流水线会显示失败，这是预期行为**
（主动报错是为了让「采集卡住」这件事在流水线列表里可见），
看到这一串就说明链式调用是通的。验证完**务必删掉这两行**，日常使用不需要它们。

#### 5.3 验证重点

1. 「采集一个分片」有 `⏳ [行业标准] 第 N/M 页` 进度输出；
2. 「回写 CSV 与进度状态」显示 `✅ 采集进度已回写远端`；
3. 「继续下一分片」或「采集完成 → 触发构建管道」被正确选中（看日志里
   `下一步 : crawl` / `下一步 : build`）；
4. 构建管道日志里出现 `standard_db 构建完成` 且归档成功。

### 不再需要跨仓库令牌

数据库只保留在本仓库内（`standard_db/`），不再有第 2 个仓库的推送，
所以**不需要额外的访问令牌或密钥仓库**。流水线内置的 `CNB_TOKEN`
对本仓库有 `repo-code:rw` 权限，`git push` 回写进度足够用。

> 若要自行把数据库分发到别处，在 `scripts/publish_db.sh` 的基础上改即可。

---

## 五、standard_db 结构

`standard_db/standards.db`，SQLite 3，约 113MB / 296,711 条：

```sql
CREATE TABLE standards (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_no            TEXT,     -- 原始标准号，如 'GB 47372-2026'
    standard_no_normalized TEXT,     -- 规范化：全角转半角、破折号统一、大写
    standard_no_compact    TEXT,     -- 去掉空格：'GB/T1234.5-2020'，便于免空格检索
    name                   TEXT,     -- 标准名称
    category               TEXT,     -- 国标强制 / 国标推荐 / 指导性技术文件 / 行业标准 / 地方标准
    is_adopted             INTEGER,  -- 是否采标 0/1
    status                 TEXT,     -- 现行 / 废止 / 即将实施 ...
    publish_date           TEXT,     -- YYYY-MM-DD
    implement_date         TEXT,     -- YYYY-MM-DD
    industry               TEXT,     -- 行业领域（仅行业标准）
    region                 TEXT,     -- 省市区（仅地方标准）
    source_file            TEXT,     -- 来源 CSV
    content_hash           TEXT,
    approval_dept          TEXT,     -- 主管部门（行业 / 地方标准，来自备案平台）
    replaces               TEXT,     -- 本标准代替的旧标准号，多个用「；」分隔（数据源申报）
    replaced_by            TEXT,     -- 代替本标准的新标准号，多个用「；」分隔（由 replaces 反向标注）
    UNIQUE (category, standard_no_normalized)
);
```

辅助对象：`meta`（构建元信息）、`standards_view`（业务字段视图）与若干索引。

### 工程建设标准

国家标准全文公开系统和行业、地方标准备案平台不收录住建部管理的工程建设标准（GB 5xxxx、JGJ、CJJ 等）。
采集任务 `engineering` 从国家工程建设标准化信息网（ccsn.org.cn）的标准查询接口取数，写入
`工程建设标准信息.csv`：标准号、标准名称、标准类型、专业领域、状态、发布日期、实施日期、代替标准、主管部门。

- 该来源只收录**现行**标准（约 3,200 条），被代替的旧版不在其中；
- 来源里的标准号不带空格（`GB50235-2010`），入库时补成 `GB 50235-2010`；
- 类别：工程国标 → `工程建设国标`，工程行标 → `工程建设行标`；产品行标、产品国标归入
  `行业标准`、`国标推荐` / `国标强制`，其中标准号已由前三个来源收录的不再重复入库；
- 石化（SH/T）、化工（HG/T）等其他部门的工程建设行标和特种设备安全技术规范（TSG）不在该来源内。

### 替代关系（schema v3）

- `replaces`、`approval_dept` 取自行业标准、地方标准备案平台列表接口本身返回的
  `reviseStdCodes`、`chargeDept`，采集时不增加请求。国家标准的列表不含这两项，目前为空。
- `replaced_by` 在构建时由 `replaces` 反向标注：某标准申报代替 A，且 A 的标准号（去空格后）
  与库内某条标准完全一致，就在 A 上记下该标准。只认数据源明确申报的关系，
  不按「同编号、年份更新」推断；旧标准号写法与库内不一致（如两位年份）时不会被标注。
- `replacements` 表登记数据源申报的每一对代替关系（旧标准号 → 新标准），不论旧标准是否在库内。
  工程建设标准只有现行版本，查旧版（如 `GB50235-1997`）被谁代替要用这张表：

  ```sql
  SELECT new_standard_no FROM replacements WHERE old_standard_no_compact = 'GB50235-1997';
  ```
- 旧版 CSV 没有「代替标准」「主管部门」两列，下一次采集行动全量重扫时会回填。

当前数据分布：

| category | 条数 |
| --- | --- |
| 地方标准 | 116,044 |
| 行业标准 | 114,487 |
| 国标推荐 | 59,301 |
| 国标强制 | 6,080 |
| 指导性技术文件 | 799 |
| **合计** | **296,711** |

### 为什么没有 FTS5

- `unicode61` 分词器会把整串中文当成一个 token，搜「移动电源」匹配不到
  「移动电源安全技术规范」，对中文几乎无效；
- 换 `trigram` 分词器能子串匹配，但 29.7 万行要多占 **约 69MB**。

改用「常规索引 + `LIKE`」：全表 `LIKE` 实测约 **50ms**，中文子串检索准确，
数据库还小了约 18MB。

### 查询示例

```sql
SELECT standard_no, name, category, status
FROM standards WHERE name LIKE '%移动电源%';

SELECT * FROM standards WHERE standard_no_compact LIKE '%GBT1234%';

SELECT standard_no, name, region, publish_date
FROM standards
WHERE category = '地方标准' AND status = '现行'
ORDER BY publish_date DESC LIMIT 20;

SELECT standard_no, name FROM standards
WHERE region = '七台河市' AND name LIKE '%沙棘%';
```

---

## 六、几个关键取舍

### 每次采集分片都会 commit 一次 CSV

断点续爬的前提是进度必须离开容器（容器被 `SIGKILL` 后文件系统就没了）。
所以每片结束把 CSV + `.crawl_state/` 推回仓库。一次采集行动约 5 个分片，
即每月约 5 次 CSV 提交。

为了让 git 增量尽可能小，**CSV 采用追加写入，不再按行业/省市区重排**——
纯追加的 delta 远小于整表重排。排序对 `build_db.py` 没有影响。

### `standard_db` 入库带来的仓库增长

| 归档方式 | 每次更新 | 一年约 |
| --- | --- | --- |
| `standards.db`（默认） | 113 MB | 1.3 GB |
| `standards.db.gz` | 27 MB | 0.3 GB |

切换方式：编辑 `.cnb.yml`，找到**构建管道**的 `env:`（第 182 行起），
把 `# DB_COMPRESS: "1"`（第 189 行）前面的 `#` 删掉即可：

```yaml
      env:
        TZ: Asia/Shanghai
        ...
        DB_COMPRESS: "1"    # ← 取消这一行的注释
```

改完提交到 `main` 即生效。`scripts/publish_db.sh` 会自动处理版本控制的切换
（`git rm --cached` 另一个文件、`git add -f` 目标文件），
所以**不需要手动改 `.gitignore`，也不会两个文件同时留在仓库里**。

两种模式的差异：

- `DB_COMPRESS=1`：仓库里只跟踪 `standards.db.gz`；容器/本地构建出来的
  未压缩 `standards.db` 仍然保留在工作区，方便直接使用。
- `DB_COMPRESS=0`：仓库里只跟踪 `standards.db`（可直接 `git clone` 出来用）。

代价是压缩模式下使用时需要先 `gunzip`：

```bash
gunzip -k standard_db/standards.db.gz     # -k 保留原压缩包
sqlite3 standard_db/standards.db "SELECT COUNT(*) FROM standards;"
```

### 采集分片串行，不并行

三个标准类型理论上可以并行抓取，但并行分片会同时往同一分支 push，
产生非快进冲突。串行 + 分片已经能在 120 分钟限制内跑完，且实现简单可靠。

---

## 七、常见问题

**Q：流水线报 `git: command not found` / 返回码 127？**

流水线镜像里没有 git。**`python:3.12-slim` 基于 `debian:bookworm-slim`，
不包含 git**；而 `sync_latest.sh`、`commit_back.sh`、`publish_db.sh` 都要用 git
做检出、回写、推送。

改用 `python:3.12`（基于 `buildpack-deps:bookworm`，自带 git）：

```yaml
      docker:
        image: python:3.12        # 不要用 python:3.12-slim
```

代价是镜像大一些（约 1GB vs 130MB），但省掉了每次 `apt-get install git`。
脚本里已经加了前置检查，镜像配错时会直接打印上面这段说明，而不是抛一句
`git: command not found`。

**Q：为什么还是 120 分钟就挂了？**
`timeout` 只能改 **Job** 超时（上限 12 小时），改不了**流水线**总时长。
分片的 `CRAWL_BUDGET_SECONDS` 默认 4200 秒（70 分钟），
加上检出、装依赖、推送，整片约 80 分钟，留了 40 分钟余量。
如果你的仓库更大导致检出变慢，把它调到 3600 或更低。

**Q：链式调用会不会无限跑下去？**
不会。三重保护：
1. 每片必须有前进（`pages_this_run > 0`），否则 `next_action=stop` 且退出码 1；
2. 最大 24 片（`--max-chunks`）；
3. 全部抓完立刻切到 `build`，不再自触发。

**Q：某页抓不到会怎么办？**
同一页连续失败 3 次（`--page-retries`）后记入 `skipped_pages` 并跳过，
保证分片继续前进。用 `python standard_crawl.py --status` 可以看到跳过页列表。

**Q：想中途看进度？**
`python standard_crawl.py --status`，或看仓库里的 `.crawl_state/state.json`。

**Q：抓了一半想先出一版数据库？**
直接手动触发 `api_trigger_build`。构建管道只读 CSV，不依赖采集是否完成。

**Q：数据抓到一半被 kill 了会丢多少？**
最多 25 页（`CRAWL_CHECKPOINT_EVERY`）。CSV 用临时文件 + `os.replace` 原子替换，
不会出现半个文件；游标也只在 CSV 落盘后更新，所以恢复时只会**重抓**几页，
不会漏抓。已实测 `kill -9` 后 CSV 完整、游标一致、续跑无重复。

**Q：想改抓取频率？**
编辑 `.cnb.yml` 里的 `"crontab: 0 3 1 * *"`：

| 需求 | 表达式 |
| --- | --- |
| 每月 1 号、15 号 03:00 | `0 3 1,15 * *` |
| 每周日凌晨 3 点 | `0 3 * * 0` |

改完 push 即生效。注意频率提高后每次都要重新全量重扫，成本会上升。

**Q：怎么把数据库同步到 VPS？**
手动触发 `api_trigger_vps`。该管道从 `web_secrets` 仓库导入部署密钥，
用 rsync 把 `standard_db/standards.db` 推到 `${DEPLOY_PATH}/shared/database/standard/standard.db`，
再通过 SSH 重启后端服务（`${BACKEND_SERVICE}`）。

---

## 八、参考文档

- [CNB 语法手册](https://docs.cnb.cool/zh/build/grammar.html)
- [CNB 定时任务](https://docs.cnb.cool/zh/build/crontab.html)
- [CNB 超时策略](https://docs.cnb.cool/zh/build/timeout.html)
- [CNB 默认环境变量](https://docs.cnb.cool/zh/build/build-in-env.html)
- [CNB 环境变量与 exports](https://docs.cnb.cool/zh/build/env.html)
- [CNB 内置任务（cnb:apply 等）](https://docs.cnb.cool/zh/build/internal-steps.html)
- [CNB 跳过流水线（`[ci skip]`）](https://docs.cnb.cool/zh/build/skip-pipeline.html)
- [CNB 访问令牌](https://docs.cnb.cool/zh/guide/access-token.html)
