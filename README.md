# Standards Collection and Audit

Collect public metadata and verify the status of Chinese national, industry and local standards against published source evidence.

## Features

- Add new standards and update existing status, date and metadata fields by standard number.
- Resume paginated collection with persistent checkpoints and request limits.
- Match complete standard identifiers and follow explicitly documented replacement relationships.
- Retain source URLs, timestamps and evidence; flag failed queries and conflicting results for review.
- Run monthly collection and audits through GitHub Actions.

## Data sources

| Source | Purpose |
| --- | --- |
| [National Public Service Platform for Standards Information](https://std.samr.gov.cn/) | Search, details and replacement relationships |
| [National Standards Full-text Disclosure System](https://openstd.samr.gov.cn/bzgk/std/) | National standard listings and status |
| [Industry Standards Registry](https://hbba.sacinfo.org.cn/) | Industry standard metadata |
| [Local Standards Registry](https://dbba.sacinfo.org.cn/) | Local standard metadata |

Documentation, command output, workflow labels and release descriptions are in English. Collected data, source field names and standard status values remain in Chinese.

## Usage

Install dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-audit.txt
```

Check collection progress:

```bash
.venv/bin/python standard_crawl.py --status
```

Validate the public query list:

```bash
.venv/bin/python standard_audit.py public_standards.csv --dry-run
```

See [Local audits](docs/local-audit.md) and [Monthly GitHub Actions](docs/github-actions.md) for details.

## Public input and privacy

The public query list contains only standard types and identifiers, deduplicated and sorted. Original Excel workbooks, private filenames, file sizes, notes and author metadata remain local and are excluded from the public export.

Audit conclusions depend on source evidence. A missing search result does **not** imply withdrawal. Unsupported standards and unclear replacement relationships require manual review. This project does not distribute full-text standards.

## Tests

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```
