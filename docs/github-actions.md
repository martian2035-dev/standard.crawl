# Monthly GitHub Actions

The default branch is `main`. Collection and auditing run independently, with manual dispatch and resumable chunks.

## Public input

`public_standards.csv` contains only `标准类型` (standard type) and `标准号` (standard number). Entries are deduplicated and sorted. Keep the original Excel workbook local.

Update the public list locally:

```bash
.venv/bin/python scripts/public_input.py prepare ./标准信息统计表.xlsx
```

The list excludes original row numbers, duplicate counts, filenames, file sizes, private notes, source links and workbook author properties. Cloud reports, caches and release attachments use this public list as their input.

## Exporting a public repository

The export script copies an explicit file allowlist into a new directory. It excludes old Git history, original workbooks, credentials, local configuration, databases and run caches.

```bash
python3 scripts/export_github.py ../standard_crawl_public
cd ../standard_crawl_public

git init -b main
git add .
git commit -m "feat: initialize standards collection and audit"

gh auth login --hostname github.com --web --git-protocol https --scopes workflow
gh auth setup-git
gh repo create standard-crawl --public --source=. --remote=origin --push
```

The destination must not already exist. Choose the repository name as needed, and maintain the public project in that directory. Cleaning current files does not remove old Git history: publish only the new directory's `main` branch, rather than mirroring old branches or tags.

## Permissions and schedule

Enable Actions under **Settings → Actions → General** and set **Workflow permissions** to **Read and write permissions**. Branch protection or rulesets must permit automated data commits. Workflows use the built-in `GITHUB_TOKEN`; no personal access token is required.

| Workflow | Asia/Shanghai time | Purpose |
| --- | --- | --- |
| Monthly standards collection | Day 2 of each month, 03:43 | Rescan national, industry and local listings; add or update by standard number |
| Monthly standards audit | Day 5 of each month, 04:17 | Verify public query entries, dates and replacement versions |
| Offline tests | Pushes and pull requests | Check collection, auditing and privacy boundaries |

Schedules use UTC in workflow files. Runs may be delayed, and schedules in inactive public repositories may need re-enabling. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Start a run manually:

```bash
gh workflow run monthly-crawl.yml --ref main
gh workflow run monthly-audit.yml --ref main
gh run list --limit 10
```

Each chunk has a 60-minute processing budget and dispatches another chunk when needed. Both workflows stop at 100 chunks per campaign. No progress, input mismatches or restricted sites pause continuation; inspect the Actions summary before resuming. Avoid starting different campaigns concurrently for the same workflow.

## Update and audit scope

Each monthly crawl rescans from page 1, retaining historical entries and updating records by standard number. A publication-date cutoff could miss withdrawals of older standards. Records that disappear from a source are not automatically classified as withdrawn.

Monthly collection updates the national, industry and local CSV files. Existing engineering data also participates in database builds; update it separately with `--only engineering`.

Each audit fixes a baseline database from the available CSV files. Supported standards require official online queries even when the baseline contains a match. International standards and unsupported identifier formats remain `Unsupported`. A completed audit means all supported entries were attempted, not that every status was confirmed.

## Results and releases

- The three collected CSV files are updated in the repository root, retaining their Chinese names and content.
- `standards-YYYY-MM` releases contain a compressed database and integrity manifest. Large databases are kept out of Git history.
  Release assets: `standards.db.gz`, `manifest.json` and ASCII-named copies of the collected CSV files (`national-standards.csv`, `industry-standards.csv`, `local-standards.csv`); GitHub strips non-ASCII characters from asset names, so the Chinese file names cannot be used there.
- `reports/standards-audited.xlsx`, `reports/audit-results.csv` and `reports/audit-summary.json` contain the public audit results.
- `audit-YYYY-MM` releases archive reports and `audit-evidence.tar.gz` with source response evidence.
- Release titles, notes and automated commit messages are in English. Tags retain their existing `standards-YYYY-MM` and `audit-YYYY-MM` format.
- Follow [Local workbook supplementation](local-audit.md#public-input-and-local-workbook-supplementation) to merge public results into a private local copy while retaining duplicate rows and private fields.

## Recovery

Crawl progress is stored in `.github_crawl_state/`. Dispatch the same `campaign` (`YYYY-MM`) to resume. Failed pages are retried, and the monthly database is published only after collection finishes.

Audit checkpoints are uploaded as `audit-checkpoint` artifacts with seven-day retention. To resume, enter the same campaign and the latest run ID containing a complete artifact as `restore_run_id`. Continuations verify input and baseline hashes and stop if the artifact is missing or the query list changed.

If the artifact expires, leave `restore_run_id` empty to start over. New campaigns use fresh caches; chunks in the same campaign share a cache. HTTP 403, 429 and CAPTCHA responses pause a site until cooldown.

The English report format uses checkpoint version 2. Checkpoints from the earlier Chinese report format cannot be resumed; start a new audit without `restore_run_id`. Local users should also start a new output directory when switching report formats. Source CSV schemas and collected values are unchanged.
