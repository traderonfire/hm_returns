"""
run.py  ·  Housemartin Returns Automation
==========================================
Orchestrates the full pipeline:
  1. Playwright: log in to all accounts, grab balances, download CSVs
  2. merge_csv.py: merge the CSVs + inject total balance
  3. irr5.py: calculate XIRR / IRR
  4. report_generator.py: write a polished HTML report

Usage:
    python run.py                  # headless (no browser window)
    python run.py --visible        # show browser (great for debugging selectors)
    python run.py --skip-scrape    # skip login/download, reuse files in hm_staging/
"""

import sys
import os
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

# ── Bootstrap: make sure we can import sibling modules ────────────────────────
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from housemartin_scraper import run_scraper, STAGING_DIR
from merge_csv import merge_csv_files
from irr5 import calculate_irr, calculate_etf_irr, create_additional_dataframe
from report_generator import generate_report
from gsheets import push_results
from pp_index import build_and_push as pp_build_and_push
import numpy as np


# ── Config ────────────────────────────────────────────────────────────────────
# Benchmark ETFs — edit freely or override via ETF_TICKERS env var (comma-separated)
DEFAULT_ETF_TICKERS = "VWRP.L,XDER.L,SLXX.L"
ETF_TICKERS = [
    t.strip()
    for t in os.getenv("ETF_TICKERS", DEFAULT_ETF_TICKERS).split(",")
    if t.strip()
]

MERGED_CSV      = HERE / "ReportsTransactionAll.csv"
REPORTS_DIR     = HERE / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
SNAPSHOTS_DIR   = HERE / "snapshots"
SNAPSHOTS_DIR.mkdir(exist_ok=True)


def stamp():
    return datetime.now().strftime("%H:%M:%S")


def step(n, desc):
    print(f"\n[{stamp()}] ── Step {n}: {desc}")


# ── Pipeline ──────────────────────────────────────────────────────────────────

def calculate_twrr(history_csv: Path, sub_account_keys: list[str]) -> dict:
    """
    Calculate cumulative True Time-Weighted Rate of Return (TWRR) from
    hm_history.csv for the portfolio and each sub-account.

    Portfolio TWRR = (hmfund_nav / 100) - 1  (direct from NAV chain)

    Per-account TWRR chains sub-period returns:
        r_t = (val_t - net_dep_t) / val_{t-1} - 1
        net_dep_t = delta_val_t - delta_pnl_t  (change in value minus change in pnl)
        TWRR = ∏(1 + r_t) - 1

    Returns {acc_key: twrr_float, 'portfolio': twrr_float}
    where acc_key matches the pattern '{holder}_{label}' used in history columns.
    Missing / insufficient data returns None for that key.
    """
    if not history_csv.exists():
        return {}
    try:
        import pandas as _pd
        hist = _pd.read_csv(history_csv, parse_dates=["date"],
                            dayfirst=True, infer_datetime_format=True)
        hist = hist.sort_values("date").reset_index(drop=True)
        if len(hist) < 2:
            return {}
    except Exception:
        return {}

    result = {}

    # Per-account TWRR: assume global TWRR (from NAV) up to the first history row
    # for that account, then chain-link from there using daily sub-period returns.
    # This gives a meaningful figure even when account history starts mid-way through.
    nav_col = next((c for c in ["NAV", "hmfund_nav"] if c in hist.columns), None)
    for acc_key in sub_account_keys:
        val_col = f"{acc_key}_final_value"
        pnl_col = f"{acc_key}_pnl"
        if val_col not in hist.columns or pnl_col not in hist.columns:
            result[acc_key] = None
            continue
        cols = [val_col, pnl_col] + ([nav_col] if nav_col else [])
        sub  = hist[cols].dropna()
        if sub.empty:
            result[acc_key] = None
            continue
        if len(sub) < 2:
            # Only one row — fall back to simple total return
            val = sub[val_col].iloc[0]
            pnl = sub[pnl_col].iloc[0]
            ni  = val - pnl
            result[acc_key] = (pnl / ni) if ni > 0 else None
            continue

        # Global TWRR assumption up to first account row
        if nav_col and nav_col in sub.columns:
            nav_first = sub[nav_col].iloc[0]
            prior = (nav_first / 100 - 1) if nav_first and nav_first > 0 else 0.0
        else:
            prior = 0.0

        # Chain-link from first account row onward
        cumulative = 1.0
        for i in range(1, len(sub)):
            val_prev = sub[val_col].iloc[i - 1]
            val_t    = sub[val_col].iloc[i]
            pnl_prev = sub[pnl_col].iloc[i - 1]
            pnl_t    = sub[pnl_col].iloc[i]
            if val_prev <= 0:
                continue
            net_dep = (val_t - val_prev) - (pnl_t - pnl_prev)
            r_t     = (val_t - net_dep) / val_prev - 1
            cumulative *= (1 + r_t)

        result[acc_key] = (1 + prior) * cumulative - 1

    return result


def main():
    skip_scrape = "--skip-scrape" in sys.argv
    headless    = "--visible"     not in sys.argv

    print("=" * 55)
    print("  Housemartin Returns Automation")
    print(f"  {datetime.now().strftime('%A, %d %B %Y  %H:%M')}")
    print("=" * 55)

    # ── Step 1: Scrape ────────────────────────────────────────────────────────
    step(1, "Logging in & scraping accounts")

    if skip_scrape:
        print("  ⚡ --skip-scrape: reusing existing files in hm_staging/")
        account_results = _load_staging_results(from_sheets=False)
    else:
        account_results = run_scraper(headless=headless)

    if not account_results:
        print("  ✗  No accounts scraped. Check credentials in .env")
        sys.exit(1)

    total_balance = sum(r["balance"] for r in account_results)
    print(f"\n  Total consolidated balance: £{total_balance:,.2f}")
    for r in account_results:
        print(f"    {r['name']}: £{r['balance']:,.2f}")
        for sa in r.get("sub_accounts", []):
            print(f"      └ {sa['label']}")

    # ── Step 2: Merge CSVs ────────────────────────────────────────────────────
    step(2, "Merging transaction CSVs")

    # Use the "all accounts" CSV per person for the consolidated XIRR merge
    csv_paths = [r["csv_path"] for r in account_results]
    merge_input_dir = HERE / "hm_merge_input"
    if merge_input_dir.exists():
        # On Windows the folder may be briefly locked by Dropbox/antivirus — retry
        import time
        for attempt in range(5):
            try:
                shutil.rmtree(merge_input_dir)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(1)
    merge_input_dir.mkdir()

    for p in csv_paths:
        shutil.copy(p, merge_input_dir / p.name)

    merge_csv_files(
        input_path=str(merge_input_dir),
        output_file=str(MERGED_CSV),
        current_value=total_balance,
    )

    # ── Step 3: Calculate IRR + all benchmark ETFs ───────────────────────────
    step(3, f"Calculating XIRR + {len(ETF_TICKERS)} benchmarks: {', '.join(ETF_TICKERS)}")

    # Portfolio IRR is calculated once (using first ETF for the base call)
    irr_result, net_investment, time_difference, df, _ = calculate_irr(
        str(MERGED_CSV), ETF_TICKERS[0]
    )

    description_col = "Description" if "Description" in df.columns else df.columns[2]
    current_value   = df.loc[df[description_col] == "Current Value", "Cash Flow"].values[0]

    df2 = create_additional_dataframe(df)

    pnl          = current_value - net_investment
    total_return = pnl / net_investment if net_investment != 0 else 0

    # Calculate metrics for each benchmark ETF
    benchmarks = []
    for ticker in ETF_TICKERS:
        print(f"  → Processing benchmark: {ticker}")
        try:
            _, _, _, df_b, etf_df = calculate_irr(str(MERGED_CSV), ticker)
            etf_irr_result    = calculate_etf_irr(df_b, etf_df)
            etf_final_value   = etf_df["ETF Value"].iloc[-1]
            etf_net_cash_flow = -df_b.loc[df_b[description_col].isin(["Deposit", "Withdraw"]), "Cash Flow"].sum()
            etf_pnl           = etf_final_value - etf_net_cash_flow
            etf_total_return  = etf_pnl / etf_net_cash_flow if etf_net_cash_flow != 0 else 0
            benchmarks.append({
                "ticker":       ticker,
                "irr":          etf_irr_result,
                "pnl":          etf_pnl,
                "final_value":  etf_final_value,
                "total_return": etf_total_return,
            })
            irr_str = f"{etf_irr_result * 100:.2f}%" if etf_irr_result else "N/A"
            print(f"     XIRR: {irr_str}  |  P&L: £{etf_pnl:,.2f}")
        except Exception as e:
            print(f"  ⚠  Could not calculate benchmark for {ticker}: {e}")
            benchmarks.append({"ticker": ticker, "irr": None, "pnl": None,
                                "final_value": None, "total_return": None})

    # Print console summary
    print(f"\n  Net Investment : £{net_investment:,.2f}")
    print(f"  P&L            : £{pnl:,.2f}")
    print(f"  Final Value    : £{current_value:,.2f}")
    print(f"  Total Return   : {total_return * 100:.2f}%")
    print(f"  XIRR           : {irr_result * 100:.2f}%" if irr_result else "  XIRR: N/A")

    # ── Step 3b: Calculate per-sub-account XIRR ─────────────────────────────
    step(3, "Calculating per sub-account XIRR")

    sub_account_results = []  # [{holder, label, irr, net_investment, current_value, pnl, total_return}]
    for r in account_results:
        tab_balances = r.get("tab_balances", {})
        tab_details  = r.get("tab_details", {})
        for sa in r.get("sub_accounts", []):
            sa_csv      = sa["csv_path"]
            sa_label    = sa["label"]
            sa_balance  = tab_balances.get(sa_label)
            sa_detail   = tab_details.get(sa_label, {})
            try:
                if sa_balance is None:
                    raise ValueError(f"No dashboard balance found for tab '{sa_label}'")

                # Re-merge this sub-account CSV with its own current value injected
                sa_merged = HERE / "hm_merge_input" / f"_sa_{r['name']}_{sa_label}.csv".replace(" ", "_")
                merge_csv_files(
                    input_path=str(sa_csv.parent),
                    output_file=str(sa_merged),
                    current_value=sa_balance,
                    single_file=str(sa_csv),
                )
                sa_irr, sa_ni, _, sa_df, _ = calculate_irr(str(sa_merged), ETF_TICKERS[0])
                sa_desc    = "Description" if "Description" in sa_df.columns else sa_df.columns[2]
                sa_cv_rows = sa_df.loc[sa_df[sa_desc] == "Current Value", "Cash Flow"]
                if sa_cv_rows.empty:
                    raise ValueError("No Current Value row after merge")
                sa_cv  = sa_cv_rows.values[0]
                sa_pnl = sa_cv - sa_ni
                sa_ret = sa_pnl / sa_ni if sa_ni != 0 else 0
                sub_account_results.append({
                    "holder":           r["name"],
                    "label":            sa_label,
                    "irr":              sa_irr,
                    "net_investment":   sa_ni,
                    "current_value":    sa_cv,
                    "pnl":              sa_pnl,
                    "total_return":     sa_ret,
                    "gross_investment": sa_detail.get("gross_investment", 0.0),
                    "cash":             sa_detail.get("cash", 0.0),
                })
                irr_str = f"{sa_irr*100:.2f}%" if sa_irr else "N/A"
                print(f"  → {r['name']} / {sa_label}: XIRR={irr_str}  £{sa_cv:,.2f}")
            except Exception as e:
                print(f"  ⚠  Sub-account XIRR failed for {r['name']}/{sa_label}: {e}")
                sub_account_results.append({
                    "holder": r["name"], "label": sa_label,
                    "irr": None, "net_investment": None,
                    "current_value": None, "pnl": None, "total_return": None,
                    "gross_investment": sa_detail.get("gross_investment", 0.0),
                    "cash":             sa_detail.get("cash", 0.0),
                })

    # Compute today's chain-linked NAV for HMFUND (needed by report + history CSV)
    try:
        from pp_index import compute_daily_nav, STATE_FILE as PP_STATE_FILE
        pp_nav_today = compute_daily_nav(current_value, net_investment) if PP_STATE_FILE.exists() else None
    except Exception:
        pp_nav_today = None

    # Portfolio TWRR from NAV chain; per-account TWRR from history CSV.
    # sa['label'] is the full site string e.g. "Regular account" / "ISA account".
    # History CSV uses {HOLDER}_{reg|ISA} e.g. A1_reg, A1_ISA.
    def _hist_key(sa):
        lbl = sa['label'].lower()
        suffix = "ISA" if "isa" in lbl else "reg"
        return f"{sa['holder']}_{suffix}"

    sa_keys  = [_hist_key(sa) for sa in sub_account_results]
    twrr_map     = calculate_twrr(SNAPSHOTS_DIR / "hm_history.csv", sa_keys)
    # Portfolio TWRR: prefer NAV-derived if available, else from twrr_map
    portfolio_twrr = (pp_nav_today / 100 - 1) if pp_nav_today and pp_nav_today > 0 else twrr_map.get("portfolio")
    print(f"  TWRR           : {portfolio_twrr * 100:.2f}%" if portfolio_twrr is not None else "  TWRR: N/A")
    # Attach per-account TWRR to sub_account_results
    for sa in sub_account_results:
        sa["twrr"] = twrr_map.get(_hist_key(sa))

    # ── Step 4: Generate HTML report ──────────────────────────────────────────
    step(4, "Generating HTML report")

    report_filename = REPORTS_DIR / f"hm_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    latest_report   = REPORTS_DIR / "hm_report_latest.html"

    generate_report(
        irr_result       = irr_result,
        net_investment   = net_investment,
        time_difference  = time_difference,
        current_value    = current_value,
        pnl              = pnl,
        total_return     = total_return,
        portfolio_twrr   = portfolio_twrr,
        benchmarks       = benchmarks,
        account_balances = account_results,
        sub_accounts     = sub_account_results,
        hmfund_nav       = pp_nav_today,
        output_path      = str(report_filename),
    )

    # Keep a "latest" copy for easy access
    # Ensure file is fully flushed to disk before Cloud Sync can see it
    import time
    try:
        with open(report_filename, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass
    time.sleep(1)   # brief pause so the OS releases any write buffers
    shutil.copy(report_filename, latest_report)
    # fsync the latest copy too
    try:
        with open(latest_report, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass

    # ── Step 5: Write snapshot txt + append to CSV history ───────────────────
    step(5, "Writing snapshot & history")

    now_dt   = datetime.now()
    date_str     = now_dt.strftime("%Y/%m/%d %H:%M")   # snapshot txt format
    date_str_gs  = now_dt.strftime("%Y-%m-%d %H:%M")    # Google Sheets format
    ts_str   = now_dt.strftime("%Y%m%d_%H%M%S")

    def fmt_pct(v):
        return f"{v * 100:.2f}%" if v is not None else "N/A"
    def fmt_gbp(v):
        return f"{v:.2f}" if v is not None else "N/A"

    # ── Ticker display names — built from ETF_LABELS env var ────────────────
    # Set ETF_LABELS in .env as: VWRP.L=Global Equities (VWRP),XDER.L=European Property (XDER)
    # Any ticker without a label entry just shows the raw ticker string.
    TICKER_NAMES = {}
    for entry in os.getenv("ETF_LABELS", "").split(","):
        entry = entry.strip()
        if "=" in entry:
            k, v = entry.split("=", 1)
            TICKER_NAMES[k.strip()] = v.strip()
    for t in ETF_TICKERS:
        TICKER_NAMES.setdefault(t, t)

    # ── Build txt content ─────────────────────────────────────────────────────
    lines = []
    lines.append("===========")
    lines.append(date_str)
    lines.append("===========")
    lines.append(f"Net Investment: {fmt_gbp(net_investment)}")
    lines.append(f"P&L: {fmt_gbp(pnl)}")
    lines.append(f"Final Value: {fmt_gbp(current_value)}")
    lines.append(f"Total return: {fmt_pct(total_return)}")
    lines.append(f"Internal rate of return (IRR): {fmt_pct(irr_result)}")
    lines.append("===========")
    lines.append("Sub-account breakdown:")
    for sa in sub_account_results:
        lines.append(f"  {sa['holder']} / {sa['label']}:")
        lines.append(f"    IRR: {fmt_pct(sa['irr'])}  |  Value: {fmt_gbp(sa['current_value'])}  |  P&L: {fmt_gbp(sa['pnl'])}")
    lines.append("===========")
    td = time_difference
    if td:
        y = td.days // 365
        m = (td.days % 365) // 30
        d = (td.days % 365) % 30
        lines.append(f"Value-Weighted Average Time Held: {y} years, {m} months, {d} days")
    df2 = create_additional_dataframe(df)
    net_twi = -df2["Product"].sum()
    lines.append(f"Net time weighted investment (equivalent investment held for 1 year): {net_twi:.2f}")
    lines.append("===========")
    for b in benchmarks:
        name = TICKER_NAMES.get(b["ticker"], b["ticker"])
        lines.append(f"{name} equivalent investment returns:")
        lines.append(f"P&L: {fmt_gbp(b['pnl'])}")
        lines.append(f"Final value: {fmt_gbp(b['final_value'])}")
        lines.append(f"Total Return: {fmt_pct(b['total_return'])}")
        lines.append(f"Internal rate of return (IRR): {fmt_pct(b['irr'])}")
        lines.append("  ")
    lines.append("===========")
    # Per-account balances
    for a in account_results:
        lines.append(f"  {a['name']}: £{a['balance']:,.2f}")
    lines.append(f"  TOTAL: £{sum(a['balance'] for a in account_results):,.2f}")

    txt_content = "\n".join(lines)

    # ── Append to CSV history ─────────────────────────────────────────────────
    import csv
    csv_path = SNAPSHOTS_DIR / "hm_history.csv"
    
    # Build header + row
    base_fields = ["date", "net_investment", "pnl", "final_value",
                   "total_return_pct", "irr_pct", "twrr_pct",
                   "avg_time_held_days", "net_time_weighted_investment",
                   "NAV"]
    bench_fields = []
    for b in benchmarks:
        t = b["ticker"].replace(".", "_")
        bench_fields += [f"{t}_pnl", f"{t}_final_value",
                         f"{t}_total_return_pct", f"{t}_irr_pct"]
    acct_fields = [a["name"].replace(" ", "_") for a in account_results]
    acct_fields += ["total_balance"]
    # Sub-account fields: one set of irr/value/pnl/return per holder+label
    sa_fields = []
    for sa in sub_account_results:
        key = f"{sa['holder']}_{sa['label']}".replace(" ", "_")
        sa_fields += [f"{key}_value", f"{key}_pnl",
                      f"{key}_total_return_pct", f"{key}_irr_pct",
                      f"{key}_twrr_pct"]
    all_fields = base_fields + bench_fields + acct_fields + sa_fields

    row = {
        "date":                        date_str,
        "net_investment":              round(net_investment, 2),
        "pnl":                         round(pnl, 2),
        "final_value":                 round(current_value, 2),
        "total_return_pct":            round(total_return * 100, 4) if total_return else None,
        "irr_pct":                     round(irr_result * 100, 4)   if irr_result   else None,
        "twrr_pct":                    round(portfolio_twrr * 100, 4) if portfolio_twrr is not None else None,
        "avg_time_held_days":          td.days if td else None,
        "net_time_weighted_investment": round(net_twi, 2),
        "NAV":                         round(pp_nav_today, 6) if pp_nav_today else None,
    }
    for b in benchmarks:
        t = b["ticker"].replace(".", "_")
        row[f"{t}_pnl"]              = round(b["pnl"], 2)          if b["pnl"]          is not None else None
        row[f"{t}_final_value"]      = round(b["final_value"], 2)  if b["final_value"]  is not None else None
        row[f"{t}_total_return_pct"] = round(b["total_return"] * 100, 4) if b["total_return"] is not None else None
        row[f"{t}_irr_pct"]          = round(b["irr"] * 100, 4)   if b["irr"]          is not None else None
    for a in account_results:
        row[a["name"].replace(" ", "_")] = round(a["balance"], 2)
    row["total_balance"] = round(sum(a["balance"] for a in account_results), 2)
    for sa in sub_account_results:
        key = f"{sa['holder']}_{sa['label']}".replace(" ", "_")
        row[f"{key}_value"]            = round(sa["current_value"], 2) if sa["current_value"] is not None else None
        row[f"{key}_pnl"]              = round(sa["pnl"], 2)           if sa["pnl"]           is not None else None
        row[f"{key}_total_return_pct"] = round(sa["total_return"] * 100, 4) if sa["total_return"] is not None else None
        row[f"{key}_irr_pct"]          = round(sa["irr"] * 100, 4)    if sa["irr"]           is not None else None
        row[f"{key}_twrr_pct"]         = round(sa["twrr"] * 100, 4)   if sa.get("twrr")      is not None else None

    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    print(f"  → History CSV  : {csv_path}")

    # ── Step 6: Push to Google Sheets ───────────────────────────────────────
    step(6, "Pushing results to Google Sheet")
    try:
        push_results(
            snapshot_date       = date_str_gs,
            net_investment      = net_investment,
            current_value       = current_value,
            pnl                 = pnl,
            total_return        = total_return,
            portfolio_twrr      = portfolio_twrr,
            irr                 = irr_result,
            account_results     = account_results,
            sub_account_results = sub_account_results,
            benchmarks          = benchmarks,
            ticker_names        = TICKER_NAMES,
        )
    except Exception as e:
        print(f"  ⚠  Google Sheets push failed (non-fatal): {e}")

    # ── Step 7: Update PP index (daily incremental) ──────────────────────────
    step(7, "Updating Portfolio Performance index")
    try:
        from pp_index import daily_update, compute_daily_nav, STATE_FILE

        if not STATE_FILE.exists():
            print("  → PP state not initialised — run 'python pp_index.py' once first")
        else:
            # Map sub_account_results to pp_index acc_keys
            # acc_key format: {HOLDER_INITIAL}_{reg|isa}
            # e.g. holder="A1", label="Regular account" → "A1_reg"
            acct_vals = {}
            acct_ni   = {}
            for sa in sub_account_results:
                holder = sa["holder"].upper()
                label  = sa["label"].lower()
                if "regular" in label:
                    acc_key = f"{holder}_reg"
                elif "isa" in label:
                    acc_key = f"{holder}_isa"
                else:
                    continue
                if sa.get("current_value") and sa.get("pnl") is not None:
                    acct_vals[acc_key] = sa["current_value"]
                    # Compute gross invested = current_value - pnl.
                    # This is pure deposited capital, independent of irr5 internals,
                    # and matches the basis used in hm_history.csv and the seed.
                    acct_ni[acc_key] = sa["current_value"] - sa["pnl"]

            # Compute today's chain-linked NAV from portfolio-level figures
            pp_nav = compute_daily_nav(
                current_value = current_value,
                current_ni    = net_investment,
            )
            if pp_nav is None:
                print("  ⚠  Could not compute PP NAV — skipping")
            else:
                print(f"  → PP NAV today: {pp_nav:.6f}")
                daily_update(
                    work_dir             = HERE,
                    nav                  = pp_nav,
                    date_str             = now_dt.strftime("%Y-%m-%d"),
                    account_values       = acct_vals,
                    account_net_invested = acct_ni,
                )
    except Exception as e:
        import traceback
        print(f"  ⚠  PP index update failed (non-fatal): {e}")
        print(traceback.format_exc())

    # ── Done ──────────────────────────────────────────────────────────────────
    print(f"\n{'=' * 55}")
    print("  ✓  All done!")
    print(f"  Report   : {report_filename}")
    print(f"  History  : {csv_path}")
    print(f"  Sheet    : {os.getenv('GSHEET_ID', '(GSHEET_ID not set)')} → {os.getenv('GSHEET_SHEET_NAME', 'HM')}")
    print(f"  PP index : HM_prices_portfolio + per-account + HM_transactions")
    print(f"{'=' * 55}\n")

    # Touch all output files at the very end to update mtime — this ensures
    # Cloud Sync (which watches mtime) picks them up after the run completes.
    import time
    time.sleep(2)   # let any pending I/O settle first
    for f in [report_filename, latest_report, csv_path]:
        try:
            p = Path(f)
            if p.exists():
                p.touch()
        except Exception:
            pass

    # Open report in default browser on Windows
    if sys.platform == "win32":
        os.startfile(str(latest_report))


def _load_staging_results(from_sheets: bool = False):
    """
    When --skip-scrape or --from-sheets is used: find CSVs already in hm_staging/
    and pair them with balances — either from Google Sheets or entered manually.

    Sheet matching: the sheet's Account column must contain the account name
    (e.g. "A1", "A2", "A3") — case-insensitive partial match is fine.
    """
    staging = STAGING_DIR
    # Only pick up the "all accounts" CSVs (not sub-account files)
    csvs = sorted(
        f for f in staging.glob("*.csv")
        if not f.stem.startswith("_sa_")
        and ("_all" in f.stem or "_transactions" in f.stem)
    )
    if not csvs:
        # Fall back to any CSV
        csvs = sorted(staging.glob("*.csv"))
    if not csvs:
        print(f"  ✗  No CSVs found in {staging}. Run without --skip-scrape first.")
        sys.exit(1)

    # Build name -> csv_path map
    name_to_csv = {}
    for csv_path in csvs:
        stem = csv_path.stem
        name = (stem.replace("_all", "")
                    .replace("_transactions", "")
                    .replace("_", " ")
                    .strip())
        name_to_csv[name] = csv_path

    print(f"  Found {len(name_to_csv)} account CSV(s) in staging: {list(name_to_csv.keys())}")

    if from_sheets:
        try:
            sheet_rows = fetch_balances()
            print(f"  → Fetched {len(sheet_rows)} rows from Google Sheet")
        except Exception as e:
            print(f"  ✗  Google Sheet fetch failed: {e}")
            sys.exit(1)

        results = []
        for name, csv_path in name_to_csv.items():
            # Match sheet row by account name (case-insensitive partial)
            match = next(
                (r for r in sheet_rows
                 if name.lower() in r["account"].lower()
                 or r["account"].lower() in name.lower()),
                None
            )
            if match is None:
                print(f"  ⚠  No sheet row matched for '{name}' — skipping")
                continue
            balance = match["balance"]
            if balance is None:
                print(f"  ⚠  Sheet row for '{name}' has no balance — skipping")
                continue
            print(f"  → {name}: £{balance:,.2f}  (from sheet: {match['account']})")
            results.append({
                "name":        name,
                "balance":     balance,
                "csv_path":    csv_path,
                "sub_accounts": [],
                "tab_balances": {},
                "tab_details":  {},
            })
        return results

    else:
        # Manual entry fallback
        results = []
        print("  Enter balances manually:")
        for name, csv_path in name_to_csv.items():
            while True:
                try:
                    val = float(
                        input(f"    Balance for {name} (£): ")
                        .replace(",", "").replace("£", "")
                    )
                    results.append({
                        "name":        name,
                        "balance":     val,
                        "csv_path":    csv_path,
                        "sub_accounts": [],
                        "tab_balances": {},
                        "tab_details":  {},
                    })
                    break
                except ValueError:
                    print("    Invalid amount, try again.")
        return results


if __name__ == "__main__":
    main()
