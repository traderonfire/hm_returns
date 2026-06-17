# Housemartin Returns Automation

Automates the full pipeline for a multi-account Housemartin property platform portfolio: logs into all accounts, scrapes balances and sub-account breakdowns, downloads transaction CSVs, merges them, calculates XIRR and TWRR, produces an HTML report, writes local snapshots, pushes results to Google Sheets, and maintains a synthetic fund (HMFUND) for Portfolio Performance import.

> **Disclaimer:** This is independent personal automation with no affiliation with Housemartin Property Limited. It is not financial advice. Use at your own risk.

> **Fragility notice:** The scraper works by interacting with Housemartin's Angular web interface directly. If Housemartin updates their frontend, the scraper may break. See [Troubleshooting](#troubleshooting) for guidance on fixing selectors.

---

## File layout

```
housemartin/
├── run.py                        ← main orchestrator — run this
├── housemartin_scraper.py        ← Playwright: login, balance scrape, CSV download
├── merge_csv.py                  ← merges per-account CSVs + injects current value
├── irr5.py                       ← XIRR / IRR calculations
├── report_generator.py           ← HTML report builder
├── gsheets.py                    ← Google Sheets push module
├── pp_index.py                   ← Portfolio Performance NAV + transaction builder
├── process_history.py            ← batch-processes historical CSV files
├── run_scraper.py                ← Docker entrypoint (calls run.py via subprocess)
├── launch_scraper.sh             ← Synology Task Scheduler shell script
├── snapshots5.txt                ← historical manual snapshots (required for PP seed)
├── service_account.json          ← Google service account key (never commit)
├── .env                          ← credentials (never commit)
└── .env.example                  ← template — copy to .env and fill in
```

Generated at runtime:

```
├── ReportsTransactionAll.csv            ← merged transaction history
├── hmfund_quotes.csv                    ← HMFUND daily NAV (PP import)
├── hmfund_transactions_seed.csv         ← PP transactions (PP import)
├── hm_pp_state.json                     ← PP incremental state
├── hm_pp_state.backup.json              ← previous day's state (auto-backup)
├── hm_pp_state_history.csv             ← rolling state log (one row per day)
├── hm_pp_seed_done.flag                 ← prevents re-running PP seed
├── hm_staging/                          ← per-account CSVs + debug screenshots
├── reports/
│   ├── hm_report_YYYYMMDD_HHMMSS.html  ← timestamped HTML report
│   └── hm_report_latest.html           ← always the most recent
└── snapshots/
    ├── hm_history.csv                  ← one row per run, grows over time
    └── hm_gsheets_log.csv              ← local mirror of every Sheets push
```

---

## Setup (one-time)

### 1. Install Python dependencies

```
pip install playwright pandas numpy scipy yfinance python-dotenv google-auth google-auth-httplib2 google-api-python-client
playwright install chromium
```

### 2. Configure credentials

```
copy .env.example .env       # Windows
# cp .env.example .env       # Mac/Linux
```

Open `.env` and fill in:

```ini
# Account credentials — accounts with blank NAME/EMAIL/PASSWORD are skipped
HM_ACCOUNT1_NAME=Account1
HM_ACCOUNT1_EMAIL=account1@example.com
HM_ACCOUNT1_PASSWORD=yourpassword

HM_ACCOUNT2_NAME=Account2
HM_ACCOUNT2_EMAIL=account2@example.com
HM_ACCOUNT2_PASSWORD=yourpassword

HM_ACCOUNT3_NAME=Account3
HM_ACCOUNT3_EMAIL=account3@example.com
HM_ACCOUNT3_PASSWORD=yourpassword

# Benchmark ETFs — any Yahoo Finance tickers
ETF_TICKERS=VWRP.L,XDER.L,VAGS.L
ETF_LABELS=VWRP.L=Global Equities (VWRP),XDER.L=European Property (XDER),VAGS.L=Global Bonds (VAGS)

# Google Sheets
GSHEET_ID=your_google_sheet_id_here
GSHEET_SHEET_NAME=HM
GSHEET_KEY_FILE=service_account.json

# Portfolio Performance
PP_ACCOUNT_TYPES=reg,isa
PP_ACCOUNT1_PP_NAME=Account1 HM
PP_ACCOUNT2_PP_NAME=Account2 HM
PP_ACCOUNT3_PP_NAME=Account3 HM
PP_STARTING_DATE=2023-01-01
PP_SPLIT_DATE=2026-03-14
```

Passwords containing `$` or other special characters should be wrapped in single quotes in `.env`.

---

## Usage

```bash
python run.py                # normal headless run
python run.py --visible      # browser visible — use when debugging
python run.py --skip-scrape  # reuse existing CSVs, enter balances manually
python run.py --pp-full      # write PP transactions every day (default: cash-flow + month-end only)
```

---

## Pipeline steps

| Step | What happens |
|------|-------------|
| 1 | Playwright logs into each account, reads per-sub-account balances (including pending withdrawals) from dashboard tabs, downloads per-sub-account CSVs and one All-accounts CSV |
| 2 | Merges All-accounts CSVs, injects total current balance as a "Current Value" row in the Cash change column (required for XIRR) |
| 3 | Calculates portfolio XIRR; per-sub-account XIRR using scraped balance as current value; benchmark ETF XIRRs |
| 4 | Produces a styled HTML report: XIRR headline, TWRR, HMFUND NAV, portfolio returns table, sub-account breakdown with TWRR and XIRR, benchmark comparison cards |
| 5 | Appends one row to `snapshots/hm_history.csv` |
| 6 | Pushes results to Google Sheets HM tab (rolling 50-run history, newest at top); a local copy of the same row is also appended to `snapshots/hm_gsheets_log.csv` as a backup |
| 7 | Updates HMFUND NAV quote and per-account transactions for Portfolio Performance |

---

## HTML Report

The report shows:

- **Hero section:** XIRR (annualised), TWRR (cumulative), HMFUND NAV — all three side by side
- **Portfolio Returns:** net invested, current value, P&L, total return, TWRR, XIRR, avg time held
- **Account Breakdown:** total balance per account holder
- **Sub-Account Breakdown:** per account — invested, cash, total value, P&L, total return, TWRR, XIRR
- **Benchmark Comparisons:** P&L, final value, total return, XIRR, outperformance vs HMFUND XIRR

---

## Returns Metrics

Three return metrics are calculated and displayed:

| Metric | What it measures | Notes |
|--------|-----------------|-------|
| **Total Return** | `P&L / Net Invested` | Simple return; can become unstable if NI is small or negative |
| **TWRR** | True Time-Weighted Rate of Return | Strips out deposit/withdrawal timing effects. Portfolio-level: `NAV/100 - 1` (exact, from the HMFUND chain). Per-account: chain-linked from `hm_history.csv` using `r_t = (val_t − net_deposit_t) / val_{t-1} − 1`, with global TWRR assumed for the pre-history period |
| **XIRR** | Internal Rate of Return (annualised) | Money-weighted; reflects deposit timing |

---

## Google Sheets integration

After every run, results are pushed to a Google Sheet tab. Rolling 50-run history, newest at top.

### Sheet columns

`Date | Current Value | Net Invested | P&L | Total Return % | TWRR % | XIRR % | [Account Balances] | [Sub-account: Invested, Cash, Total, P&L, Return %, XIRR %, TWRR %] | [ETF columns]`

### Setup

1. [Google Cloud Console](https://console.cloud.google.com/) → create project → enable **Google Sheets API** and **Google Drive API**
2. Create a **Service Account** → download JSON key → rename to `service_account.json`, place alongside `run.py`
3. Share your Google Sheet with the service account email (Editor access)
4. Create a tab named `HM` (or set via `GSHEET_SHEET_NAME`)
5. The Sheet ID is the long string in the URL: `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

The push is non-fatal — if it fails, the rest of the run completes normally. A local copy of every pushed row is also saved to `snapshots/hm_gsheets_log.csv`, independent of whether the Sheets push succeeds, so you always have a local backup of this data.

---

## Portfolio Performance integration

`pp_index.py` builds a synthetic fund **HMFUND** for import into [Portfolio Performance](https://www.portfolio-performance.info/). One security represents the entire platform; per-account accuracy is achieved through a two-transaction daily scheme that correctly reflects each account's performance.

### HMFUND NAV formula (chain-linking)

```
CF_t     = NI_t − NI_{t−1}                  # net deposit (+) or withdrawal (-)
units_t  = units_{t−1} + CF_t / NAV_{t−1}   # adjust unit count for flows
NAV_t    = Value_t / units_t                 # pure performance, no flow distortion
```

- Starting: `PP_STARTING_DATE`, NAV = 100.00
- Immune to deposit/withdrawal size effects
- Income captured as NAV appreciation (accumulating fund behaviour)
- Interpolated linearly between sparse snapshot points

### Daily transaction scheme

Each recorded day produces two transactions per account:

```
Buy:  today's full value at today's NAV              → establishes new unit count
Sell: (today_value − net_deposit) at pp_units        → captures pure performance, strips flows
```

Where `net_deposit = delta_value − delta_pnl` and `pp_units` = the shares PP holds from the last *written* transaction (not the running internal count).

- **Deposit day:** sell proceeds stripped of deposit → no artificial P&L gain
- **Withdrawal day:** same formula in reverse → no artificial P&L loss
- **Flat day:** buy and sell are identical → transactions skipped automatically
- **Skipped day:** internal state (units, NI) advances silently; `pp_units` stays frozen at the last written value so the next written sell always references shares PP actually holds

### Transaction frequency

By default, transactions are only written on days where they matter for TWRR accuracy:

| Condition | Transactions written |
|-----------|---------------------|
| Cash flow detected (`abs(delta_ni) > £10`) | ✓ |
| Last calendar day of the month | ✓ |
| `--pp-full` flag | ✓ always |
| Pure performance day (no flow, not month-end) | ✗ skipped |

The NAV quote is always written to `hmfund_quotes.csv` regardless, keeping the price history continuous.

This reduces the PP database from ~3,650 transaction pairs per account per year to ~30–50, while preserving TWRR accuracy. To regenerate the full history with the lean rules: `python pp_index.py --reseed`. To use daily transactions throughout: `python pp_index.py --reseed --pp-full`.

### Transaction format

Comma-delimited CSV with columns: `Date,Type,Value,Shares,Quote,ISIN,Ticker Symbol,Securities Account`

- `Type` is always `Buy` or `Sell`
- Dates in `YYYY-MM-DD` format
- Import as **Portfolio Transactions** in PP, tick **Convert to Delivery**
- `ISIN`: `XX000HM00001`, `Ticker Symbol`: `HM`

### State file and recovery

`hm_pp_state.json` stores the last known NAV, total units, net invested, per-account units/NI, and `pp_units` (the unit count PP holds from the last written transaction — this may differ from `units` on days when transactions are skipped). A backup is automatically written to `hm_pp_state.backup.json` before each update. `hm_pp_state_history.csv` keeps a full daily log of all state values for easy manual recovery.

**To restore state to a previous date:**
1. Find the target date row in `hm_pp_state_history.csv`
2. Copy values back into `hm_pp_state.json` and set `last_date` to that date
3. Delete any transaction/quote rows after that date from the CSVs
4. Re-run

### Idempotency

The pipeline is safe to run multiple times on the same day:
- `daily_update()` checks `last_date` in state and skips if already ran today
- `push_results()` in gsheets checks for an existing row with today's date and skips if found
- Identical buy/sell pairs (no price movement, no flow) are suppressed automatically

### Setup (one-time)

**1. Configure `.env`** — set `PP_ACCOUNT_TYPES`, `PP_ACCOUNT{n}_PP_NAME`, `PP_STARTING_DATE`, `PP_SPLIT_DATE`

**2. Prepare `snapshots5.txt`** — historical manual snapshots, one per date:

```
2024-03-15
===========
Net Investment: 85113.72
Final Value: 90777.96
```

**3. Run the seed:**

```bash
python pp_index.py
```

Produces `hmfund_quotes.csv`, `hmfund_transactions_seed.csv`, `hm_pp_state.json`.

**4. Publish Google Sheets tabs as CSV** (File → Share → Publish to web → CSV) and copy the URLs for `HM_pp_quotes` and `HM_pp_transactions`.

**5. In Portfolio Performance:**
- Create security `HMFUND` (ISIN `XX000HM00001`, ticker `HM`), starting price 100.00 on `PP_STARTING_DATE`
- Historical prices → Add from URL → `HM_pp_quotes` URL (comma separator)
- Create one securities account per sub-account matching names in `.env`
- Import `HM_pp_transactions` as Portfolio Transactions (comma separator), tick Convert to Delivery

**To re-seed from scratch:** delete `hm_pp_seed_done.flag`, `hmfund_quotes.csv`, `hmfund_transactions_seed.csv`, restore `hm_pp_state.json` to a known-good baseline, then:

```bash
python pp_index.py           # lean mode: cash-flow days + month-end only (recommended)
python pp_index.py --pp-full # write transactions every day
python pp_index.py --reseed  # force re-seed even if flag file exists
```

---

## Docker / NAS scheduling

For daily automated runs on a Synology NAS:

```
housemartin-docker/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── run_scraper.py          ← calls run.py via subprocess
```

`launch_scraper.sh` is triggered by Synology Task Scheduler at 7am. It:
1. Runs the Docker container
2. Touches all output files to trigger Synology Cloud Sync (inotify doesn't fire for container writes on the NAS filesystem)
3. Refreshes `hm_report_latest.html` with a delete+copy to ensure Cloud Sync picks it up
4. Keeps only the last 30 log files

Set `COMPOSE_DIR` and `DROPBOX_DIR` in your `.env` file (see `.env.example`) — `launch_scraper.sh` reads them automatically, so no manual editing of the script is needed.

**Rebuild the Docker image when:** `requirements.txt` or `Dockerfile` changes. Python files on the mounted volume are picked up without a rebuild.

---

## History CSV columns

`snapshots/hm_history.csv` — one row per run:

```
date, net_investment, pnl, final_value, total_return_pct, irr_pct, twrr_pct,
avg_time_held_days, net_time_weighted_investment, NAV,
{ETF}_pnl, {ETF}_final_value, {ETF}_total_return_pct, {ETF}_irr_pct,
{NAME}_reg_final_value, {NAME}_reg_pnl, {NAME}_reg_total_return, {NAME}_reg_XIRR,
{NAME}_ISA_final_value, ...
```

---

## Troubleshooting

**Scraper times out or fails**
Run with `--visible`. Check `hm_staging/*_debug.png` screenshots and `filter_html_dump.txt` (written automatically on failure) to find updated selectors.

**Wrong balance detected**
The scraper finds the card labelled "Total platform balance" by walking the DOM. If the site layout changes, update `read_total_platform_balance_text()` in `housemartin_scraper.py`.

**Sub-account balance is zero**
The scraper retries zero-balance tabs automatically (up to 2 retries with a 1-second pause). If it still fails, a debug screenshot is saved as `balance_retry_failed.png`.

**Sub-account balance sum doesn't match total**
Same retry logic — the scraper detects the mismatch and re-reads the diverging tabs.

**CSV download never triggers**
The site generates Blob downloads client-side. The scraper intercepts via a JS hook on `URL.createObjectURL`. It retries 3 times. Check `*_export_fallback_debug.png` if all attempts fail.

**Angular dropdown (ng-select) not responding**
Run with `--visible`. The account filter requires two selections (specific account → Apply → All accounts → Apply) to force Angular change detection.

**XIRR calculation fails**
Check that the Current Value row is in column I (Cash change), not column J (Balance), in `ReportsTransactionAll.csv`.

**Google Sheets push fails**
Check `service_account.json` is present, the sheet is shared with the service account email (Editor), and `GSHEET_ID` / `GSHEET_SHEET_NAME` match. The error is non-fatal — the run still completes, and the same row is saved locally to `snapshots/hm_gsheets_log.csv` regardless.

**Sub-account "Invested" shows £0 in Google Sheets or the report**
The scraper's `gross_investment` field can occasionally return 0 if the "Gross investment" card isn't found on the dashboard. `run.py` falls back to `current_value - cash` in this case, which is always accurate. If you see this with an older run, it predates the fix.

**PP transactions are wrong after a failed run**
Restore `hm_pp_state.json` from `hm_pp_state.backup.json` (or from a row in `hm_pp_state_history.csv`), delete the bad transaction rows from the CSVs, and re-run.

**PP final values differ between abridged and complete transaction sets**
This was caused by the sell on the first post-skip day using the internal running unit count rather than the units PP actually holds. Fixed in the current version — `pp_units` in state tracks what PP last saw and is used for sell share counts, while the internal `units` continues advancing through skipped days for accurate delta calculations. If you see this with an older transaction file, re-seed with `python pp_index.py --reseed`.

**PP transactions show identical buy/sell pairs**
This means the pipeline ran twice on the same day with the same values. The idempotency guard in `daily_update()` prevents this on subsequent runs — delete the duplicate rows from the CSV and re-run with the correct state.

**Key selectors that may break with site updates:**

| Selector | Used for |
|----------|----------|
| `a.summary-account-select` | Dashboard tab click |
| `'total platform balance'` text | Balance card |
| `app-select-account .ng-select-container` | Transaction filter dropdown |
| `.ng-option` | Dropdown options |
| `button:has-text("Export to CSV")` | Export button |
| `div.block-ui-wrapper.root.active` | Loading spinner |
| `[class*="uf-"]` | Userflow popup suppression |

---

## Processing historical data

If you have existing merged CSVs from before this automation (`ReportsTransactionAllYYYYMMDD.csv`):

```bash
python process_history.py                          # CSVs in current folder
python process_history.py path/to/history/folder  # specify folder
```

Produces `hm_historical_xirr.csv` with one row per file.

---

## License

MIT
