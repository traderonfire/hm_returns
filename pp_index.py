"""
pp_index.py  —  Housemartin → Portfolio Performance data builder
================================================================
NAV is computed by chain-linking, stripping out external cash flows:

    CF_t     = NI_t - NI_{t-1}
    units_t  = units_{t-1} + CF_t / NAV_{t-1}
    NAV_t    = Value_t / units_t

This is the standard fund NAV formula — performance-only, immune to
deposit/withdrawal size effects.

Starting point: 2023-08-30, NAV=100.0, units = Value/100 = 7594.46

Outputs (local CSV + Google Sheets):
  hmfund_quotes.csv          — Date,Close
  hmfund_transactions_seed.csv — Date,Type,Value,Shares,Quote,ISIN,Ticker Symbol,Securities Account
  hm_pp_state.json           — running state for daily incremental updates
"""

import os, sys, re, json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from gsheets import _get_service, SHEET_ID

STARTING_NAV  = 100.0
STARTING_DATE = pd.Timestamp(os.getenv("PP_STARTING_DATE", "2023-01-01"))
SPLIT_DATE    = pd.Timestamp(os.getenv("PP_SPLIT_DATE",    "2026-03-14"))
STATE_FILE   = HERE / "hm_pp_state.json"
BACKUP_FILE  = HERE / "hm_pp_state.backup.json"
QUOTES_FILE  = HERE / "hmfund_quotes.csv"
TXN_FILE     = HERE / "hmfund_transactions_seed.csv"

# ACCOUNTS — one entry per sub-account.
# Keys must match the acc_key pattern used in run.py (holder_subtype).
# Each entry needs:
#   securities_account — name of the PP securities account
#   hist_val           — column name in hm_history.csv for this account's value
#   hist_xirr          — column name in hm_history.csv for this account's XIRR
#   csv_file           — filename of the per-account CSV in hm_staging/
#   pp_account         — same as securities_account (kept for legacy compatibility)
#   pp_cash            — deposit account name in PP (can match securities_account)
def _build_accounts() -> dict:
    """
    Build the ACCOUNTS dict from .env variables.

    For each account defined in .env (HM_ACCOUNT1_NAME etc.) and each
    sub-account type in PP_ACCOUNT_TYPES, constructs the full account config.

    .env variables used:
        HM_ACCOUNT1_NAME=Name1     ← short identifier used in filenames/columns
        HM_ACCOUNT2_NAME=Name2
        HM_ACCOUNT3_NAME=Name3
        PP_ACCOUNT_TYPES=reg,isa   ← sub-account types (default: reg,isa)

        # Optional: override the PP securities account display name prefix
        # If not set, defaults to "{HM_ACCOUNTn_NAME} HM"
        PP_ACCOUNT1_PP_NAME=Name1 HM
        PP_ACCOUNT2_PP_NAME=Name2 HM
        PP_ACCOUNT3_PP_NAME=Name3 HM
    """
    accounts = {}
    types_str = os.getenv("PP_ACCOUNT_TYPES", "reg,isa")
    sub_types = [t.strip().lower() for t in types_str.split(",") if t.strip()]

    for i in range(1, 4):
        name = os.getenv(f"HM_ACCOUNT{i}_NAME", "").strip()
        if not name:
            continue
        pp_prefix = os.getenv(f"PP_ACCOUNT{i}_PP_NAME", f"{name} HM").strip()

        for sub in sub_types:
            acc_key = f"{name}_{sub}"
            # Column names in hm_history.csv follow the pattern set by run.py:
            #   {NAME}_reg_final_value  or  {NAME}_ISA_final_value
            sub_col = "ISA" if sub == "isa" else sub
            hist_val  = f"{name}_{sub_col}_final_value"
            hist_pnl  = f"{name}_{sub_col}_pnl"
            hist_xirr = f"{name}_{sub_col}_XIRR"
            # Staging CSV filename
            sub_file = "isa" if sub == "isa" else "regular"
            csv_file  = f"{name}_{sub_file}_acc.csv"
            # PP display name — ISA stays all-caps, other types are title-cased
            sub_label = sub.upper() if sub.lower() == "isa" else sub.title()
            sec_acc   = f"{pp_prefix} {sub_label}"

            accounts[acc_key] = {
                "securities_account": sec_acc,
                "hist_val":           hist_val,
                "hist_pnl":           hist_pnl,
                "hist_xirr":          hist_xirr,
                "csv_file":           csv_file,
                "pp_account":         sec_acc,
                "pp_cash":            f"{sec_acc} (GBP)",
            }

    if not accounts:
        raise ValueError(
            "No accounts found — set HM_ACCOUNT1_NAME etc. in your .env file"
        )
    return accounts


ACCOUNTS = _build_accounts()

HMFUND_ISIN   = "XX000HM00001"
HMFUND_TICKER = "HM"


# ── 1. Parse data sources ─────────────────────────────────────────────────────

def parse_snapshots_txt(path: Path) -> pd.DataFrame:
    rows = []
    cur_date = cur_val = cur_ni = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = re.match(r'^(\d{4}-\d{2}-\d{2})$', line)
            if m:
                if cur_date and cur_val is not None:
                    rows.append({"date": pd.Timestamp(cur_date),
                                 "value": cur_val, "net_invested": cur_ni})
                cur_date = m.group(1); cur_val = cur_ni = None
            m2 = re.search(r'(?:Final Value|Current Value)\s*[:\s]+([0-9,]+\.?\d*)', line)
            if m2: cur_val = float(m2.group(1).replace(",",""))
            m3 = re.search(r'Net Investment\s*[:\s]+([0-9,]+\.?\d*)', line)
            if m3: cur_ni  = float(m3.group(1).replace(",",""))
    if cur_date and cur_val is not None:
        rows.append({"date": pd.Timestamp(cur_date), "value": cur_val, "net_invested": cur_ni})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def parse_history_csv(path: Path) -> pd.DataFrame:
    # Use the python engine so rows with unquoted commas inside value fields
    # (e.g. numbers formatted as "1,234.56") do not cause C-parser tokeniser errors.
    # on_bad_lines="warn" drops genuinely corrupt rows with a warning rather than
    # crashing; in practice the issue is usually a single rogue comma in one cell.
    df = pd.read_csv(path, engine="python", on_bad_lines="warn")
    df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=True).dt.normalize()
    df = df.rename(columns={"final_value": "value", "net_investment": "net_invested"})
    return df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


# ── 2. Chain-link NAV across a DataFrame ─────────────────────────────────────

def chain_link_nav(df: pd.DataFrame, prev_nav: float, prev_units: float,
                   prev_ni: float) -> pd.DataFrame:
    """
    Add 'nav' and 'units' columns to df using the chain-linking formula.
    df must have columns: date, value, net_invested  (sorted ascending).
    prev_* are the values from the last known point before df starts.
    """
    df = df.copy()
    navs, units_list = [], []
    p_nav, p_units, p_ni = prev_nav, prev_units, prev_ni
    for _, row in df.iterrows():
        cf     = row.net_invested - p_ni
        units  = p_units + cf / p_nav
        nav    = row.value / units
        navs.append(nav)
        units_list.append(units)
        p_nav, p_units, p_ni = nav, units, row.net_invested
    df["nav"]   = navs
    df["units"] = units_list
    return df


def build_full_nav_series(snap_df: pd.DataFrame,
                          hist_df: pd.DataFrame) -> pd.DataFrame:
    """
    1. Prepend a synthetic starting row on STARTING_DATE (2023-01-01):
       - All net_invested of the first snapshot is treated as deposited on this date.
       - NAV = 100.0, units = net_invested_first / 100.
       - Value on starting date = units × 100 = net_invested_first.
    2. Chain-link from that starting row through all snapshots.
    3. Bridge into history CSV from the last snapshot.
    4. Merge, deduplicate, interpolate daily.
    Returns DataFrame(date, nav, units, value, net_invested) at daily frequency.
    """
    first_snap = snap_df.iloc[0]

    # Synthetic starting row: NAV=100, all money treated as invested on 2023-01-01
    start_units = first_snap.net_invested / STARTING_NAV
    start_value = first_snap.net_invested   # value = units × NAV = NI at start
    start_row   = pd.DataFrame([{
        "date":         STARTING_DATE,
        "value":        start_value,
        "net_invested": first_snap.net_invested,
    }])

    # Prepend start row to snapshots, then chain-link
    snap_extended = pd.concat([start_row, snap_df], ignore_index=True)
    snap_linked   = chain_link_nav(snap_extended,
                                   prev_nav=STARTING_NAV,
                                   prev_units=start_units,
                                   prev_ni=first_snap.net_invested)
    # Fix row 0: no CF on starting date, so nav=100, units=NI/100
    snap_linked.at[0, "nav"]   = STARTING_NAV
    snap_linked.at[0, "units"] = start_units

    # Recompute from row 1 onwards with corrected row-0 values
    prev_nav, prev_units, prev_ni = STARTING_NAV, start_units, first_snap.net_invested
    for i in range(1, len(snap_linked)):
        row    = snap_extended.iloc[i]
        cf     = row.net_invested - prev_ni
        units  = prev_units + cf / prev_nav
        nav    = row.value / units
        snap_linked.at[i, "nav"]   = nav
        snap_linked.at[i, "units"] = units
        prev_nav, prev_units, prev_ni = nav, units, row.net_invested

    last = snap_linked.iloc[-1]

    # Step 2: chain-link history CSV from last snapshot
    hist_linked = chain_link_nav(hist_df,
                                  prev_nav=last.nav,
                                  prev_units=last.units,
                                  prev_ni=last.net_invested)

    # Step 3: merge — history takes precedence
    combined = pd.concat([
        snap_linked[["date","nav","units","value","net_invested"]],
        hist_linked[["date","nav","units","value","net_invested"]],
    ]).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)

    # Step 4: fill daily gaps by linear interpolation
    daily = pd.DataFrame({"date": pd.date_range(combined.date.min(),
                                                  combined.date.max(), freq="D")})
    daily = daily.merge(combined, on="date", how="left")
    daily[["nav","units","value","net_invested"]] = \
        daily[["nav","units","value","net_invested"]].interpolate(method="linear")

    return daily.reset_index(drop=True)


# ── 3. NAV lookup with interpolation ─────────────────────────────────────────

def get_nav_on(nav_series: pd.DataFrame, target: pd.Timestamp) -> float:
    exact = nav_series[nav_series.date == target]
    if not exact.empty:
        return float(exact.iloc[0].nav)
    before = nav_series[nav_series.date <= target]
    after  = nav_series[nav_series.date >= target]
    if before.empty: return float(nav_series.iloc[0].nav)
    if after.empty:  return float(nav_series.iloc[-1].nav)
    d0, n0 = before.iloc[-1].date, float(before.iloc[-1].nav)
    d1, n1 = after.iloc[0].date,   float(after.iloc[0].nav)
    span   = max((d1 - d0).days, 1)
    frac   = (target - d0).days / span
    return round(n0 + frac * (n1 - n0), 6)


# ── 4 & 5. Historical per-account transactions (Approach 3) ──────────────────
#
# Uses actual cash flow dates from per-account CSVs.
# Back-calculates implied account balance at each flow date using the
# per-account XIRR from the first post-split snapshot.
#
# At each cash flow date:
#   1. FV of all flows to that date at solved rate  = implied balance
#   2. cash-flow Buy/Sell at NAV  (the external deposit/withdrawal)
#   3. rebalance pair to hit implied balance with net cash = 0:
#        Buy  units_new at NAV
#        Sell units_old at implied_balance / units_old
#
# A "seed already done" flag (SEED_DONE_FILE) prevents re-running the
# expensive historical seed on every daily run. Use --reseed to force it.

SEED_DONE_FILE = HERE / "hm_pp_seed_done.flag"

# Derived automatically from ACCOUNTS — no personal data here
LEDGER_FILES = {k: v["csv_file"]        for k, v in ACCOUNTS.items()}
XIRR_COLS    = {k: v["hist_xirr"]       for k, v in ACCOUNTS.items()}
VAL_COLS     = {k: v["hist_val"]        for k, v in ACCOUNTS.items()}


def fv_of_flows(flows_df: pd.DataFrame, target_date: pd.Timestamp, r: float) -> float:
    """FV at target_date of all flows up to that date, compounded at annual rate r."""
    past  = flows_df[flows_df["date"] <= target_date]
    total = 0.0
    for _, row in past.iterrows():
        t = (target_date - row["date"]).days / 365.25
        total += float(row["amount"]) * (1 + r) ** t
    return total


def solve_rate(flows_df: pd.DataFrame, target_date: pd.Timestamp,
               target_value: float, hint_r: float) -> float:
    """Find annual rate r such that fv_of_flows(target_date, r) = target_value."""
    from scipy.optimize import brentq
    def obj(r):
        return fv_of_flows(flows_df, target_date, r) - target_value
    if abs(obj(hint_r)) < 0.10:
        return hint_r
    try:
        return brentq(obj, -0.99, 10.0, xtol=1e-10, maxiter=200)
    except ValueError:
        return hint_r


def build_historical_transactions(staging_dir: Path, hist_df: pd.DataFrame,
                                   nav_series: pd.DataFrame) -> list[dict]:
    """
    Build per-account transactions for the pre-split period using Approach 3.
    """
    # Get first post-split row that has per-account data
    xirr_present = [c for c in XIRR_COLS.values() if c in hist_df.columns]
    first_post   = hist_df.dropna(subset=xirr_present, how="all").iloc[0]
    split_date   = first_post["date"]
    print(f"  → Historical seed: back-calculating to {split_date.date()}")

    txns      = []
    end_units = {}   # acc_key -> units held at end of pre-split period

    for acc_key, fname in LEDGER_FILES.items():
        fpath = staging_dir / fname
        if not fpath.exists():
            print(f"  ⚠  {fname} not found — skipping {acc_key}")
            continue

        acc     = ACCOUNTS[acc_key]
        sec_acc = acc["securities_account"]

        # Load and aggregate same-day flows
        df = pd.read_csv(fpath)
        df["date"] = pd.to_datetime(df["Date"], dayfirst=True).dt.normalize()
        cash_col   = next(c for c in df.columns if "cash" in c.lower())
        df["amount"] = pd.to_numeric(
            df[cash_col].astype(str).str.replace(r"[£,()\s]", "", regex=True),
            errors="coerce"
        )
        flows_all = (df[df["Description"].isin(["Deposit", "Withdraw"])]
                     .groupby("date")["amount"].sum()
                     .reset_index()
                     .sort_values("date"))

        if flows_all.empty:
            print(f"  ⚠  No flows for {acc_key}")
            continue

        # Pre-split flows only for solving the rate
        flows_pre = flows_all[flows_all["date"] < split_date].copy()

        if flows_pre.empty:
            print(f"  ⚠  No pre-split flows for {acc_key} — skipping historical seed")
            continue

        # Solve for the rate that reproduces split-date value
        target_val  = float(first_post[VAL_COLS[acc_key]])
        stored_xirr = float(first_post[XIRR_COLS[acc_key]]) / 100.0
        r = solve_rate(flows_pre, split_date, target_val, stored_xirr)
        simulated   = fv_of_flows(flows_pre, split_date, r)
        print(f"  → {acc_key}: rate={r*100:.4f}%  "
              f"simulated=£{simulated:,.2f}  target=£{target_val:,.2f}  "
              f"error=£{abs(simulated-target_val):,.2f}")

        # Forward-simulate balance at each pre-split flow date
        prev_units = 0.0

        for _, row in flows_pre.iterrows():
            d   = row["date"]
            amt = float(row["amount"])
            nav = get_nav_on(nav_series, d)

            # Implied balance after this flow
            bal_after   = fv_of_flows(flows_pre, d, r)
            units_new   = bal_after / nav

            # 1. Cash-flow transaction at NAV
            kind = "Buy" if amt > 0 else "Sell"
            txns.append({
                "date": d, "type": kind,
                "value": round(abs(amt), 2),
                "shares": round(abs(amt) / nav, 6),
                "quote": round(nav, 6),
                "securities_account": sec_acc,
            })

            # 2. Rebalance pair to hit implied balance (net cash = 0)
            # After the cash flow, units_after_flow = prev_units + amt/nav
            units_after_flow = prev_units + amt / nav
            rebal_delta      = units_new - units_after_flow

            if abs(rebal_delta) > 0.0001 and units_after_flow > 0.0001:
                p_sell = bal_after / units_after_flow
                txns += [
                    {"date": d, "type": "Buy",
                     "value": round(bal_after, 2), "shares": round(units_new, 6),
                     "quote": round(nav, 6), "securities_account": sec_acc},
                    {"date": d, "type": "Sell",
                     "value": round(bal_after, 2), "shares": round(units_after_flow, 6),
                     "quote": round(p_sell, 6), "securities_account": sec_acc},
                ]

            prev_units = units_new

        n = len([t for t in txns if t.get("securities_account") == sec_acc])
        print(f"    {acc_key}: {n} transactions generated  "
              f"(end units={prev_units:.4f})")
        end_units[acc_key] = prev_units

    return txns, end_units


# ── 6. Daily per-account rebalances (post-split) ─────────────────────────────

def build_rebalance_transactions(hist_df: pd.DataFrame,
                                  nav_series: pd.DataFrame,
                                  hist_end_units: dict) -> list[dict]:
    """
    Post-split transactions from hm_history.csv.

    hist_end_units: {acc_key: units} — units held at end of pre-split period.

    For each day, two transactions per account:
      Buy:  today's full value at today's NAV  → establishes correct unit count
      Sell: previous units at (today_value − delta_ni) / prev_units
            → captures pure performance; deposit/withdrawal stripped from P&L

    delta_ni is derived from the total portfolio net_invested column, allocated
    to individual accounts by comparing each account's value change against the
    expected pure-performance change. Accounts whose value change matches pure
    NAV performance get delta_ni=0; any residual in the total is assigned to the
    account(s) that show an anomalous value change.
    """
    txns       = []
    per_acct   = hist_df[hist_df.date >= SPLIT_DATE].sort_values("date").reset_index(drop=True)
    total_ni_col = "net_invested" if "net_invested" in hist_df.columns else None

    # prev_state: acc_key -> {units, val}
    prev_state = {k: {"units": v, "ni": None}
                  for k, v in hist_end_units.items()}

    prev_total_ni = None

    for _, row in per_acct.iterrows():
        nav        = get_nav_on(nav_series, row.date)
        total_ni   = float(row[total_ni_col]) if total_ni_col else None

        # Total deposit/withdrawal today across whole portfolio
        delta_ni_total = 0.0
        if total_ni is not None and prev_total_ni is not None:
            delta_ni_total = total_ni - prev_total_ni

        # Per-account NI = final_value - pnl (exact, no approximation needed)
        # delta_ni per account derived from day-to-day change in this figure.
        acct_ni_today = {}
        for acc_key, acc in ACCOUNTS.items():
            vcol = acc["hist_val"]
            pcol = acc["hist_pnl"]
            if vcol in row.index and pcol in row.index \
                    and not pd.isna(row[vcol]) and not pd.isna(row[pcol]):
                acct_ni_today[acc_key] = float(row[vcol]) - float(row[pcol])

        for acc_key, acc in ACCOUNTS.items():
            vcol = acc["hist_val"]
            if vcol not in row.index or pd.isna(row[vcol]):
                continue
            val_today   = float(row[vcol])
            if val_today <= 0:
                continue
            units_today = val_today / nav
            sec_acc     = acc["securities_account"]

            ps        = prev_state.get(acc_key, {})
            units_old = ps.get("units", 0.0)
            ni_old    = ps.get("ni", None)
            ni_today  = acct_ni_today.get(acc_key)

            if units_old < 0.0001:
                # Opening buy — no prior holding
                prev_state[acc_key] = {"units": units_today, "ni": ni_today}
                txns.append({"date": row.date, "type": "Buy",
                             "value": round(val_today, 2),
                             "shares": round(units_today, 6),
                             "quote": round(nav, 6),
                             "securities_account": sec_acc})
                continue

            # Per-account delta_ni from exact NI figures (val - pnl)
            if ni_today is not None and ni_old is not None:
                delta_ni = ni_today - ni_old
            else:
                delta_ni = 0.0

            # Two-transaction scheme — always sell all previous units.
            # sell_proceeds = val - delta_ni strips the flow (deposit or withdrawal)
            # from P&L. units_sell = units_old always: for ISA transfers/withdrawals,
            # units move across accounts rather than being redeemed at a prior price,
            # so adjusting units_sell would distort the sell quote.
            sell_proceeds = round(val_today - delta_ni, 2)
            sell_quote    = round(sell_proceeds / units_old, 6)
            buy_val       = round(val_today, 2)
            buy_units     = round(units_today, 6)
            sell_units    = round(units_old, 6)
            buy_quote     = round(nav, 6)

            if (buy_val == sell_proceeds and buy_units == sell_units
                    and buy_quote == sell_quote):
                prev_state[acc_key] = {"units": units_today, "ni": ni_today}
                continue

            txns += [
                {"date": row.date, "type": "Buy",
                 "value": buy_val, "shares": buy_units, "quote": buy_quote,
                 "securities_account": sec_acc},
                {"date": row.date, "type": "Sell",
                 "value": sell_proceeds, "shares": sell_units, "quote": sell_quote,
                 "securities_account": sec_acc},
            ]

            prev_state[acc_key] = {"units": units_today, "ni": ni_today}

        prev_total_ni = total_ni

    return txns


# ── 7. Google Sheets helpers ──────────────────────────────────────────────────

def ensure_sheet_tab(svc, title: str):
    meta     = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]
    if title not in existing:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
        ).execute()
        print(f"  → Created tab '{title}'")


def write_sheet(svc, tab: str, df: pd.DataFrame):
    ensure_sheet_tab(svc, tab)
    values = [list(df.columns)] + df.astype(str).values.tolist()
    svc.spreadsheets().values().clear(spreadsheetId=SHEET_ID, range=f"{tab}!A:Z").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{tab}!A1",
        valueInputOption="USER_ENTERED", body={"values": values}
    ).execute()
    print(f"  → {len(df)} rows → '{tab}'")


# ── 8. Main seed ──────────────────────────────────────────────────────────────

def build_and_push(work_dir: Path = None):
    work_dir    = work_dir or HERE
    staging_dir = work_dir / "hm_staging"
    snap_path   = work_dir / "snapshots5.txt"
    hist_path   = work_dir / "snapshots" / "hm_history.csv"

    for p, label in [(snap_path,"snapshots5.txt"), (hist_path,"hm_history.csv")]:
        if not p.exists():
            print(f"  ⚠  {label} not found at {p} — skipping PP build")
            return

    print("  Parsing sources...")
    snap_df = parse_snapshots_txt(snap_path)
    hist_df = parse_history_csv(hist_path)
    print(f"  → {len(snap_df)} snapshot rows, {len(hist_df)} history rows")

    nav_series = build_full_nav_series(snap_df, hist_df)
    last = nav_series.iloc[-1]
    print(f"  → NAV series: {nav_series.date.min().date()} → {nav_series.date.max().date()}")
    print(f"  → Latest: NAV={last.nav:.6f}  units={last.units:.4f}")
    # Show a few key NAV values for verification
    for chk_date in [STARTING_DATE, pd.Timestamp("2023-08-30"), pd.Timestamp("2023-10-09")]:
        row = nav_series[nav_series.date == chk_date]
        if not row.empty:
            r = row.iloc[0]
            print(f"  → {chk_date.date()}: NAV={r.nav:.6f}  units={r.units:.4f}")

    # Quotes
    quotes_df = nav_series[["date","nav"]].copy()
    quotes_df["date"] = quotes_df["date"].dt.strftime("%Y-%m-%d")
    quotes_df.columns = ["Date","Close"]
    quotes_df["Close"] = quotes_df["Close"].round(4)
    quotes_df.to_csv(QUOTES_FILE, sep=",", index=False)
    print(f"  → Quotes: {QUOTES_FILE} ({len(quotes_df)} rows)")

    # Transactions — historical seed (approach 3) + post-split rebalances
    hist_txns, end_units = build_historical_transactions(staging_dir, hist_df, nav_series)
    rebal_txns           = build_rebalance_transactions(hist_df, nav_series, end_units)

    print(f"  → Historical: {len(hist_txns)}  Rebalance: {len(rebal_txns)}")

    all_txns = hist_txns + rebal_txns
    txn_df = pd.DataFrame(all_txns).sort_values(["date","securities_account","type"])
    txn_df["date"]   = txn_df["date"].dt.strftime("%Y-%m-%d")
    txn_df["isin"]   = HMFUND_ISIN
    txn_df["ticker"] = HMFUND_TICKER
    # Reorder columns to PP import format
    txn_df = txn_df[["date","type","value","shares","quote","isin","ticker","securities_account"]]
    txn_df.columns   = ["Date","Type","Value","Shares","Quote","ISIN","Ticker Symbol","Securities Account"]
    txn_df.to_csv(TXN_FILE, sep=",", index=False)
    print(f"  → Transactions: {TXN_FILE} ({len(txn_df)} rows)")

    # State file
    state = {
        "last_date":    str(last.date.date()),
        "nav":          round(float(last.nav),    6),
        "total_units":  round(float(last.units),  6),
        "net_invested": round(float(last.net_invested), 2),
        "units":        {},
        "acct_ni":      {},
    }
    last_hist = hist_df.iloc[-1]
    nav_last  = float(last.nav)
    for acc_key, acc in ACCOUNTS.items():
        vcol = acc["hist_val"]
        if vcol in last_hist.index and not pd.isna(last_hist[vcol]):
            v = float(last_hist[vcol])
            state["units"][acc_key]  = round(v / nav_last, 6)
            state["acct_ni"][acc_key] = round(v, 2)
    import shutil as _shutil
    if STATE_FILE.exists():
        _shutil.copy2(STATE_FILE, BACKUP_FILE)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  → State: {STATE_FILE}")

    # Push to Sheets
    if SHEET_ID:
        try:
            svc = _get_service()
            write_sheet(svc, "HM_pp_quotes",        quotes_df)
            write_sheet(svc, "HM_pp_transactions",   txn_df)
            print("  ✓ Pushed to Google Sheets")
        except Exception as e:
            print(f"  ⚠  Sheets push failed (non-fatal): {e}")


# ── 9. Daily incremental update (called from run.py) ─────────────────────────

def daily_update(work_dir: Path = None, *, nav: float, date_str: str,
                  account_values: dict, account_net_invested: dict):
    """
    nav                  — today's chain-linked NAV (computed by run.py)
    date_str             — 'YYYY-MM-DD'
    account_values       — {acc_key: current_value}
    account_net_invested — {acc_key: net_invested}
    """
    work_dir = work_dir or HERE
    if not STATE_FILE.exists():
        print("  ⚠  hm_pp_state.json not found — run 'python pp_index.py' first")
        return

    prev  = json.load(open(STATE_FILE))
    txns  = []

    for acc_key, acc in ACCOUNTS.items():
        val  = account_values.get(acc_key, 0.0)
        ni   = account_net_invested.get(acc_key, 0.0)
        if not val:
            continue

        units_today = val / nav
        units_prev  = prev.get("units", {}).get(acc_key, 0.0)
        ni_prev     = prev.get("acct_ni", {}).get(acc_key, 0.0)
        delta_ni    = ni - ni_prev      # deposit (+) or withdrawal (-) today
        sec_acc     = acc["securities_account"]

        # ── Two-transaction scheme ────────────────────────────────────────
        # Buy:  today's full value at today's NAV  → establishes new unit count
        # Sell: previous units at a price that captures pure performance only
        #       sell_proceeds = today_value − delta_ni  (deposit/withdrawal not P&L)
        #       sell_quote    = sell_proceeds / units_prev
        #
        # On a pure-performance day (delta_ni ≈ 0):
        #   sell_proceeds = today_value  →  sell_quote reflects actual movement
        # On a deposit day:
        #   sell_proceeds strips the deposit so P&L is unaffected
        # On a withdrawal day:
        #   delta_ni < 0  →  sell_proceeds > today_value  (correct, mirrors inflow)
        #
        # Net cash = buy_value − sell_proceeds = delta_ni  (matches actual flow)
        # Robust to inaccuracies in delta_ni: even if delta_ni drifts slightly,
        # the unit count (val/nav) is always correct and P&L tracks NAV movement.

        if units_prev > 0.0001:
            # Always sell all previous units; sell_proceeds = val - delta_ni
            # strips the flow from P&L whether deposit or withdrawal.
            sell_proceeds = round(val - delta_ni, 2)
            sell_quote    = round(sell_proceeds / units_prev, 6)
            buy_val       = round(val, 2)
            buy_units     = round(units_today, 6)
            sell_units    = round(units_prev, 6)
            buy_quote     = round(nav, 6)

            # Skip if buy and sell are identical — happens when NAV is unchanged
            # and there's no flow (e.g. re-run on same day with same scraped values).
            # Identical pairs produce zero P&L and zero net units change, so they
            # are noise in PP and should not be recorded.
            if (buy_val == sell_proceeds and buy_units == sell_units
                    and buy_quote == sell_quote):
                continue

            txns += [
                {"date": date_str, "type": "Buy",
                 "value": buy_val, "shares": buy_units, "quote": buy_quote,
                 "securities_account": sec_acc},
                {"date": date_str, "type": "Sell",
                 "value": sell_proceeds, "shares": sell_units, "quote": sell_quote,
                 "securities_account": sec_acc},
            ]
        else:
            # First day for this account — opening Buy only
            txns.append({"date": date_str, "type": "Buy",
                         "value": round(val, 2),
                         "shares": round(units_today, 6),
                         "quote": round(nav, 6),
                         "securities_account": sec_acc})

        prev.setdefault("units",{})[acc_key]   = round(units_today, 6)
        prev.setdefault("acct_ni",{})[acc_key] = round(ni, 2)

    # Unit balance check
    total_units = sum(prev["units"].values())
    total_val   = sum(account_values.values())
    implied     = total_val / nav if nav else 0
    diff        = abs(total_units - implied)
    if diff > 0.1:
        print(f"  ⚠  Unit imbalance: Σ={total_units:.3f}  value/NAV={implied:.3f}  diff={diff:.3f}")
    else:
        print(f"  ✓ Unit check OK: Σunits={total_units:.3f} ≈ value/NAV={implied:.3f}")

    # Update state — recalculate total_units from per-account units
    prev["nav"]          = round(nav, 6)
    prev["last_date"]    = date_str
    prev["net_invested"] = round(sum(account_net_invested.values()), 2)
    prev["total_units"]  = round(sum(prev["units"].values()), 6)
    import shutil as _shutil
    if STATE_FILE.exists():
        _shutil.copy2(STATE_FILE, BACKUP_FILE)
    with open(STATE_FILE, "w") as f:
        json.dump(prev, f, indent=2)

    # Append to local files
    with open(QUOTES_FILE, "a") as f:
        f.write(f"{date_str},{round(nav,4)}\n")
    if txns:
        with open(TXN_FILE, "a") as f:
            for t in txns:
                f.write(f"{t['date']},{t['type']},{t['value']},{t['shares']},"
                        f"{t['quote']},{HMFUND_ISIN},{HMFUND_TICKER},"
                        f"{t['securities_account']}\n")
        print(f"  → {len(txns)} PP transactions for {date_str}")
    else:
        print(f"  → No PP transactions for {date_str} (NAV-only day)")

    # Push to Sheets
    if SHEET_ID:
        try:
            svc       = _get_service()
            quotes_df = pd.read_csv(QUOTES_FILE, sep=",")
            txn_df    = pd.read_csv(TXN_FILE,    sep=",")
            write_sheet(svc, "HM_pp_quotes",       quotes_df)
            write_sheet(svc, "HM_pp_transactions",  txn_df)
        except Exception as e:
            print(f"  ⚠  PP Sheets push failed (non-fatal): {e}")


# ── 10. NAV calculator for run.py ─────────────────────────────────────────────

def compute_daily_nav(current_value: float, current_ni: float) -> float:
    """
    Called from run.py to get today's chain-linked NAV.
    Reads previous state from hm_pp_state.json.
    Uses sum of per-account units as the authoritative unit count
    (total_units may be stale if daily_update ran on an older version).
    """
    if not STATE_FILE.exists():
        return None
    prev     = json.load(open(STATE_FILE))
    prev_nav = prev["nav"]
    prev_ni  = prev["net_invested"]
    # Use sum of per-account units for accuracy; fall back to total_units
    per_acct_units = prev.get("units", {})
    prev_units = sum(per_acct_units.values()) if per_acct_units else prev["total_units"]
    cf    = current_ni - prev_ni
    units = prev_units + cf / prev_nav
    return round(current_value / units, 6)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build/update HMFUND PP data")
    parser.add_argument("--reseed", action="store_true",
                        help="Force re-run of historical seed even if already done")
    parser.add_argument("work_dir", nargs="?", default=".",
                        help="Working directory (default: current)")
    args = parser.parse_args()

    work = Path(args.work_dir).resolve()
    seed_flag = work / "hm_pp_seed_done.flag"

    if args.reseed and seed_flag.exists():
        seed_flag.unlink()
        print("  → Seed flag cleared — will re-run full historical seed")

    build_and_push(work)
