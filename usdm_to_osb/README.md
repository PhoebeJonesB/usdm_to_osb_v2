# USDM 4.0 to OpenStudyBuilder Uploader

Takes a USDM 4.0 JSON file and uploads its full contents into an OpenStudyBuilder (OSB) instance via the OSB REST API. All codelist and term UIDs are resolved dynamically from the OSB frontend at runtime — nothing is hardcoded.

---

## What it uploads

| Section | OSB entities created |
|---|---|
| Study + metadata | Study record, identification, high-level design, population, intervention, description |
| Arms | Study arms from `studyDesigns.studyArms` |
| Epochs | Epochs from schedule timelines |
| Elements | Screening, treatment, follow-up elements with type classification |
| Visits | Visits with timing, anchor logic, and encounter mapping |
| Objectives & endpoints | Objective templates, study objectives, endpoint templates, study endpoints |
| Eligibility criteria | Inclusion/exclusion criteria via templates |
| Activities | Activity matching (fuzzy + biomedical concept synonym), group/subgroup creation |

---

## Prerequisites

```
Python 3.9+
pip install requests beautifulsoup4
```

`beautifulsoup4` is used to strip HTML from criteria text. If not installed, it falls back to a regex-based stripper.

---

## Setup — after cloning

Before running, you must supply your OSB instance URLs and credentials. There are no working defaults — the placeholders in the code (`https://your-osb-instance/api`, `https://your-idp-instance`) are intentionally blank and must be replaced with your real values.

The recommended way to do this is via the config file (see below). Alternatively, set environment variables:

| Variable | Description |
|---|---|
| `OSB_API_URL` | OSB API base URL, e.g. `https://myosb.example.com/api` |
| `OSB_IDP_URL` | OAuth2 IDP URL, e.g. `https://myosb-idp.example.com` |
| `OSB_CLIENT_ID` | OAuth2 client ID (default: `osbidp`) |
| `OSB_CLIENT_SECRET` | OAuth2 client secret |
| `OSB_USERNAME` | OSB login email |
| `OSB_PASSWORD` | OSB password |

---

## Setup — credentials config file

Credentials are never hardcoded. Before running, copy the template and fill in your values:

```bash
copy config_template.json config.json
```

Then edit `config.json`:

```json
{
    "api_base_url": "https://your-osb-instance/api",
    "idp_url":      "https://your-idp-instance",
    "client_id":    "osbidp",
    "client_secret": "YOUR_CLIENT_SECRET",
    "username":     "you@example.com",
    "password":     "YOUR_PASSWORD",
    "project_number": "CDISC DEV"
}
```

> `config.json` contains real secrets — do not commit it to version control.
> The template `config_template.json` (with placeholder values) is safe to commit.

**Credential resolution order** — the script looks for each value in this order and uses the first one it finds:

1. `--config config.json` (recommended)
2. Environment variables: `OSB_CLIENT_SECRET`, `OSB_USERNAME`, `OSB_PASSWORD`, `OSB_CLIENT_ID`, `OSB_API_URL`, `OSB_IDP_URL`
3. CLI flags: `--client-secret`, `--username`, `--password`, etc.
4. Interactive prompt at runtime (for any secrets still missing)

---

## Quick Start

Run all commands from inside the `usdm_to_osb` directory, or use absolute paths.

### Step 1 — Validate your USDM file (no credentials needed)

```bash
python -m usdm_to_osb --usdm "C:\full\path\to\study.json"
```

Reports which sections are present, missing, or optional. Exits before touching the API if validation fails.

### Step 2 — Upload

Once validation passes, re-run with `--config`:

```bash
python -m usdm_to_osb \
    --usdm   "C:\full\path\to\study.json" \
    --config "C:\full\path\to\config.json"
```

The script will:
1. Re-validate the USDM file (safety check)
2. Prompt `Validation passed. Proceed with upload? (yes/no)`
3. Authenticate via OAuth2 using the credentials from `config.json`
4. Fetch all CT terms from the OSB frontend and build dynamic lookup indexes
5. Create the study and upload every section in order

> Always use **absolute paths** for `--usdm` and `--config` to avoid "file not found" errors regardless of which directory you run the command from.

---

## CLI Reference

```
python -m usdm_to_osb [options]

Required:
  --usdm FILE          Path to the USDM 4.0 JSON file

Credential options (use --config for simplest setup):
  --config FILE        Path to JSON config file with all credentials
  --api-url URL        OSB API base URL
  --idp-url URL        OAuth2 IDP URL
  --client-id ID       OAuth2 client ID (default: osbidp)
  --client-secret STR  OAuth2 client secret
  --username EMAIL     OSB login email
  --password STR       OSB password
```

### Examples

```bash
# Recommended — config file with absolute paths
python -m usdm_to_osb \
    --usdm   "C:\full\path\to\devices.json" \
    --config "C:\full\path\to\usdm_to_osb\config.json"

# Using environment variables instead of a config file
set OSB_USERNAME=you@example.com
set OSB_CLIENT_SECRET=your_secret
set OSB_PASSWORD=your_password
python -m usdm_to_osb --usdm "C:\full\path\to\study.json"

# Override a single URL while still loading credentials from config
python -m usdm_to_osb \
    --usdm    "C:\full\path\to\study.json" \
    --config  "C:\full\path\to\usdm_to_osb\config.json" \
    --api-url "https://staging.example.com/api"
```

---

## Validation output explained

```
  [OK]       study.name
  [OK]       versions[0]
  [OK]       titles                              (4 items)
  [OK]       studyIdentifiers                    (2 items)
  [OK]       studyDesigns[0]
  [OK]       arms                                (3 items)
  [OK]       epochs                              (5 items)
  [SKIP]     blindingSchema                      MISSING — section will be skipped
  [CRITICAL] studyType                           MISSING — upload will be blocked

  RESULT: FAILED — fix the CRITICAL sections above, then re-run.
```

| Tag | Meaning |
|---|---|
| `[OK]` | Section present with data |
| `[SKIP]` | Section missing but optional — will be skipped during upload |
| `[CRITICAL]` | Section missing and required — upload is blocked until fixed |

---

## How dynamic CT resolution works

The `CTResolver` (`ct_resolver.py`) eliminates all hardcoded UIDs:

1. On startup, fetches every CT term from `GET /ct/terms` (paginated, ~1 000 items/page)
2. Builds three in-memory indexes keyed by **codelist name**:
   - `concept_id` — matches CDISC codes like `C98388`
   - `sponsor_preferred_name` — exact name match
   - `submission_value` — submission value match
3. Every mapper calls `ct.resolve("Codelist Name", code="C98388", decode="Interventional Study")`
4. Resolution tries in order: concept_id → exact name → exact submission value → fuzzy match

If your OSB instance uses different codelist names, update `FIELD_TO_CODELIST` in `ct_resolver.py`.

---

## Logging

Every run creates a timestamped log file in the working directory:

```
usdm_upload_YYYYMMDD_HHMMSS.log
```

The log captures:
- Every CT term resolution (input code/decode → resolved UID)
- Every API call (URL, status code, response body on failure)
- Section-level summaries (`Arms: 3/3 created`, `Criteria: 28/31 created`)
- Final upload summary

---

## Package structure

```
usdm_to_osb/
├── __init__.py            Package marker
├── __main__.py            Entry point — handles CLI args and calls run.py
├── run.py                 Core upload engine
│                            Phases: validate → confirm → authenticate → upload
├── config.py              Config dataclass + OAuth2 TokenManager
│                            Manages token acquisition and auto-refresh
├── config_template.json   Credential template — copy to config.json and fill in
├── api_client.py          Thin REST wrapper (GET / POST / PATCH + pagination)
├── ct_resolver.py         Dynamic CT term resolver
│                            Fetches all terms at runtime, resolves by
│                            concept_id / name / submission value / fuzzy match
├── validation.py          Checks every required USDM section before upload
├── mappers.py             Translates USDM 4.0 structures → OSB API request payloads
├── uploaders.py           POST/PATCH functions for each entity type
│                            (study, arms, epochs, visits, objectives, criteria, activities)
├── main.py                Alternative CLI with validate/upload/list-codelists subcommands
└── README.md              This file
```

---

## Tested with

| File | Arms | Epochs | Visits | Objectives | Criteria |
|---|---|---|---|---|---|
| `CDISC_Pilot_Study.json` | 3 | 5 | 12 | 6 | 31 |
| `Alexion_NCT04573309_Wilsons.json` | 1 | 4 | 50 | 14 | — |
| `EliLilly_NCT03421379_Diabetes.json` | 2 | 5 | 7 | 6 | — |
| `observational.json` | 2 | 4 | 6 | 2 | — |
