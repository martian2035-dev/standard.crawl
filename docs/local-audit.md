# Local standards audits

`standard_audit.py` verifies complete standard identifiers, status, publication and implementation dates, and other public fields. When a withdrawn standard has an explicit successor relationship, it follows that relationship and verifies the successor's status and dates.

The original workbook is read-only. Original columns and duplicate file records are retained; audit values are appended instead of replacing original values. Workbook notes are unverified input, not instructions or evidence.

## Run an audit

From the repository root:

```bash
# Install local dependencies into an existing environment.
uv pip install --python .venv/bin/python -r requirements-local.txt

# Validate input without requests or output files.
.venv/bin/python standard_audit.py ./标准信息统计表.xlsx --dry-run

# Try the first 20 input rows.
.venv/bin/python standard_audit.py ./标准信息统计表.xlsx --limit 20

# Audit all rows, reusing unexpired query caches.
.venv/bin/python standard_audit.py ./标准信息统计表.xlsx

# Select a standard; --standard can be repeated.
.venv/bin/python standard_audit.py ./标准信息统计表.xlsx --standard 'GB 150-1998'

# Retry failures, conflicts and unmatched queries.
.venv/bin/python standard_audit.py ./标准信息统计表.xlsx --retry-failed

# Refresh all selected queries.
.venv/bin/python standard_audit.py ./标准信息统计表.xlsx --refresh

# Export records already saved locally without requests.
.venv/bin/python standard_audit.py ./标准信息统计表.xlsx --export-only
```

Input may be `.xlsx` or UTF-8 CSV. The first row must contain `标准号` (standard number); headers must be unique and nonempty. Excel worksheet selection is automatic, or use `--sheet` with its name. `--limit` counts original rows, not unique standards. Duplicate identifiers retain separate rows and share query results.

## Local output

The default output directory is `local_audit/`:

| File | Content |
| --- | --- |
| `audit-results.csv` | All input rows plus verified fields, current versions and dates, sources, conclusions and notes; unprocessed rows are `Not collected` |
| `audit-records.jsonl` | Complete processed records, source observations, field provenance and replacement chains |
| `audit-data.sqlite3` | Persisted row results and query caches; each completed row is committed immediately |
| `evidence/` | Original HTML/JSON response bytes named by SHA256; provenance records include path, hash, URL, request parameters and fetch time |
| `run-summary.json` | Input hash, cumulative and current-run counts, confirmed status counts and generation time |

Pipeline runs also export `audit-results.xlsx` with `Audit results`, `Field evidence` and `Replacement relationships` sheets. Report labels and conclusions are English; collected values and original input headers remain Chinese.

Results retain input row numbers. SQLite identifies rows by input-file SHA256 plus row number, preventing old results from being assigned to changed input files.

Use `--output-dir /path/to/local-output` to change the destination. It must not overwrite the repository root, `standard_db/` or `.crawl_state/`. Add custom in-repository destinations to `.gitignore`. The default directory is ignored, and local audit commands do not upload workbooks or results. Start a fresh output directory when upgrading from the earlier Chinese report format.

## Sources and verification rules

| Source argument | Site | Purpose |
| --- | --- | --- |
| `samr` | [National Public Service Platform](https://std.samr.gov.cn/) | Official national, industry and local searches, details and directed replacement relationships |
| `national` | [National Standards Full-text Disclosure System](https://openstd.samr.gov.cn/) | National listings, details, status, adoption flags and dates |
| `industry` | [Industry Standards Registry](https://hbba.sacinfo.org.cn/) | Official industry status, dates and metadata |
| `local` | [Local Standards Registry](https://dbba.sacinfo.org.cn/) | Official local status, dates and metadata |
| `csres` | [CSRES](http://www.csres.com/) | Third-party cross-checks and supplementary metadata; validate a supplied detail URL or search by identifier |

All sources are enabled by default. Use `--sources samr,national,industry,local` for official sources only.

1. **Match the complete identifier.** Normalize full-width characters, spaces and dashes. Expand known recommended-standard prefixes such as GBT to GB/T while preserving numbers, parts and years. Do not guess ambiguous prefixes such as GBZ. Confirm the identifier again on the detail page; titles and substring matches are insufficient.
2. **Keep evidence for each field.** Record source URL, request parameters, fetch time and response snapshot. Original workbook links are search hints, not automatically evidence.
3. **Prefer official values.** Keep conflict notes when official and third-party values disagree. Leave a field empty when equally authoritative sources conflict.
4. **Follow explicit replacement direction.** A statement that a standard replaces an older one identifies a predecessor, not a successor. Do not infer replacements from newer years, similar numbers or the workbook's existing current-version field.
5. **Verify successor status.** Only confirmed current (`现行`) terminal versions enter `Verified current versions`. Versions awaiting implementation are excluded. Preserve all branches and flag partial replacement, missing branches, cycles, conflicting evidence or depth beyond ten levels.
6. **Separate publication and implementation dates.** Map approval/publication dates according to source labels, including successor dates. Never substitute implementation dates for publication dates.
7. **Keep missing evidence and failures visible.** Missing results, unreachable sites, CAPTCHAs and layout changes do not imply withdrawal. Keep valid official listing evidence even if detail lookup fails; unavailable fields stay empty.
8. **Preserve private file metadata locally.** Filenames, sizes, notes and folder classifications come from the workbook and cannot be verified by public pages. Keep standard subject areas separate from economic industry classifications.

`Conclusion` distinguishes `Verified by official sources`, `Third-party sources only`, `Manual review required`, `Unconfirmed`, `Unsupported` and `Not collected`. Baseline-only screening uses `Baseline match (not checked online)` or `No baseline match (online check pending)`. Official verification means the status has official evidence, not that all fields are complete or every source succeeded.

These domestic sources do not fully cover international standards such as ASME and ISO. International identifiers and unsupported formats remain visible for review. Enterprise, association, special-equipment, metrology and occupational-health standards may require their original publisher. A missing search result does not establish that no valid standard exists.

## Cache, rate limits and failures

Requests are serial, with a default one-second minimum interval, 15-second timeout and two attempts. Three consecutive network failures pause a site for the current run. The collector does not bypass CAPTCHAs or access restrictions.

Query caches last seven days by default, including failed and unmatched queries. `--retry-failed` retries failures, conflicts and unconfirmed statuses; `--refresh` forces new queries. Cached results retain their original evidence timestamps. Duplicate input identifiers still produce all original rows.

`Ctrl+C` exports completed records. After a forced shutdown, committed SQLite results remain available for resumption or `--export-only`.

Exit codes: `0` for completion, input validation or export-only; `1` for input/storage errors; `2` when no processed row has a confirmed status; `130` for user interruption. Exit code `0` does not mean every standard or replacement was confirmed; inspect conclusions and notes.

## Public input and local workbook supplementation

Keep the original workbook local and publish only the deduplicated query list:

```bash
.venv/bin/python scripts/public_input.py prepare ./标准信息统计表.xlsx
```

`public_standards.csv` includes only `标准类型` and `标准号`. It excludes original row numbers, duplicate counts, filenames, sizes, notes, links and author properties. Monthly GitHub audits read only this public list.

Download the public audit CSV and merge it into a local workbook copy by standard number:

```bash
.venv/bin/python scripts/public_input.py merge ./标准信息统计表.xlsx ./reports/audit-results.csv
```

The result is `local_audit/standards-audited.xlsx`. It preserves duplicate rows, private fields, formulas and styles. Missing results are `Not collected`, and the original workbook is never overwritten.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Offline fixtures cover exact matching, replacement direction, partial and multilevel replacements, pending versions, cycles, source priority, conflict handling, date separation, cache retries, duplicate preservation and safe exports.
