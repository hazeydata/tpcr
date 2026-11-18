# Configuration

This directory contains configuration files and templates for the Theme Park Wait Time Data Pipeline.

## Queue-Times Lookup

**`queue_times_entity_mapping.csv`** — Master lookup from queue-times.com ride IDs to TouringPlans `entity_code`. Used by `src/get_wait_times_from_queue_times.py`. Columns: `entity_code`, `park_code`, `queue_times_id`, `queue_times_name`, `touringplans_name`. UH (queue-times park 66) is mapped as of TPCR-ACCURACY-001 because the TouringPlans S3 UH stream ended 2026-10-14; this only affects NEW queue-times ingests and does not re-key rows already stored as `USH*`.

## Output Base (config.json)

**`config.json`** (copy from an example) is the **single source for the pipeline output path**. It is gitignored.

- **`config.example.json`** — Template for Windows/generic. Copy to `config.json` and set `output_base`.
- **`config.linux.example.json`** — Template for Linux (e.g. Dropbox under home). Copy to `config.json` and set `output_base` to your path (e.g. `~/TouringPlans.com Dropbox/.../theme-park-crowd-report`).

- **`output_base`**: Absolute path where the pipeline writes: `fact_tables/`, `dimension_tables/`, `state/`, `logs/`, etc.  
  Example (Windows): `D:\Dropbox (TouringPlans.com)\stats team\pipeline\hazeydata\theme-park-crowd-report`  
  Example (Linux): `/home/user/TouringPlans.com Dropbox/.../theme-park-crowd-report`

**Setup**:

1. Copy `config.example.json` or `config.linux.example.json` to `config.json`.
2. Set `output_base` to your chosen output folder (e.g. your Dropbox pipeline folder).

If `config.json` is missing or has no `output_base`, the code falls back to the same default used in `config.example.json`. All of the following use this: 5am/7am ETL, 6am dimension fetch, `run_queue_times_loop.ps1`, `validate_wait_times.py`, `report_wait_time_db.py`, and the dimension/queue-times Python scripts when run without `--output-base`.

## Configuration Methods

### Command-Line Override

You can override the output base for a single run:

```powershell
python src/get_tp_wait_time_data_from_s3.py --props wdw,dlr --output-base "D:\Path"
```

### Environment Variables

AWS credentials can be configured via environment variables:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_DEFAULT_REGION`

**Why**: Standard AWS authentication method, works with AWS CLI and other tools.

### AWS Credentials File

AWS credentials can be stored in `~/.aws/credentials`:

```
[default]
aws_access_key_id = YOUR_KEY
aws_secret_access_key = YOUR_SECRET
```

**Why**: Secure way to store credentials, used by AWS CLI and boto3.

## Example

See `config.example.json` for the template. Copy it to `config.json` and set `output_base` to your output folder.
