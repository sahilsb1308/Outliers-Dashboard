"""
Shopify Sales Outliers  –  Swiss Beauty
Uses Shopify GraphQL Bulk Operations API: server-side export, no pagination limits.
Runs two bulk jobs (current quarter + same period LY) then writes two Google Sheet tabs.
"""

import sys, math, time, json, os, requests, gspread
from datetime import datetime, date, timedelta, timezone, timedelta as td
from collections import defaultdict

IST = timezone(td(hours=5, minutes=30))
from google.oauth2.service_account import Credentials

# ── CONFIG — reads from env vars (CI) or falls back to local config.py ────────
try:
    import config as _cfg
    _SHOPIFY_STORE   = getattr(_cfg, "SHOPIFY_STORE",   "swiss-beauty-dev.myshopify.com")
    _TOKEN_API_URL   = getattr(_cfg, "TOKEN_API_URL",   "https://backgroundprocessor.swiss-custom.site/api/public/token/generate")
    _TOKEN_API_KEY   = getattr(_cfg, "TOKEN_API_KEY",   "")
    _SHEET_ID        = getattr(_cfg, "SHEET_ID",        "")
    _SERVICE_ACCOUNT = getattr(_cfg, "SERVICE_ACCOUNT", "")
except ImportError:
    _SHOPIFY_STORE   = ""
    _TOKEN_API_URL   = "https://backgroundprocessor.swiss-custom.site/api/public/token/generate"
    _TOKEN_API_KEY   = ""
    _SHEET_ID        = ""
    _SERVICE_ACCOUNT = ""

SHOPIFY_STORE   = os.environ.get("SHOPIFY_STORE",   _SHOPIFY_STORE)
TOKEN_API_URL   = os.environ.get("TOKEN_API_URL",   _TOKEN_API_URL)
TOKEN_API_KEY   = os.environ.get("TOKEN_API_KEY",   _TOKEN_API_KEY)
TOKEN_SCOPES    = ["read_all_orders", "read_products", "read_customers", "read_inventory", "read_orders"]
SHEET_ID        = os.environ.get("SHEET_ID",        _SHEET_ID)
SERVICE_ACCOUNT = os.environ.get("SERVICE_ACCOUNT_PATH", _SERVICE_ACCOUNT)
GQL_URL         = f"https://{SHOPIFY_STORE}/admin/api/2024-01/graphql.json"
SCOPES          = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
HEADERS         = {"Content-Type": "application/json"}  # populated at runtime by main()

def get_shopify_token():
    r = requests.post(TOKEN_API_URL, headers={"Authorization": TOKEN_API_KEY, "Content-Type": "application/json"}, json={"scopes": TOKEN_SCOPES})
    r.raise_for_status()
    token = r.json()["data"]["token"]
    print(f"  Token refreshed: {token[:12]}...")
    return token
# ── BULK OPERATIONS ───────────────────────────────────────────────────────────

BULK_QUERY = """
mutation BulkQuery($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
"""

ORDER_QUERY_TPL = """
{{
  orders(query: "created_at:>={start}T00:00:00+05:30 created_at:<={end}T23:59:59+05:30 financial_status:paid") {{
    edges {{
      node {{
        id
        createdAt
        lineItems {{
          edges {{
            node {{
              product {{ id legacyResourceId title }}
              variant {{ id sku }}
              title
              quantity
              originalUnitPriceSet {{ shopMoney {{ amount }} }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

STATUS_QUERY = """
{ currentBulkOperation { id status errorCode url objectCount } }
"""


def gql(payload):
    r = requests.post(GQL_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def cancel_running_bulk():
    """Cancel any in-progress bulk operation and wait until it's gone."""
    for _ in range(30):
        data   = gql({"query": STATUS_QUERY})
        op     = (data.get("data") or {}).get("currentBulkOperation") or {}
        status = op.get("status")
        if not status or status in ("COMPLETED", "FAILED", "CANCELED"):
            return
        if status in ("RUNNING", "CREATED"):
            cancel = """mutation { bulkOperationCancel(id: "%s") { bulkOperation { status } } }""" % op["id"]
            gql({"query": cancel})
        # CANCELING or just cancelled — wait for it to finish
        print(f"  Waiting for previous bulk op to clear ({status}) …")
        time.sleep(5)


def run_bulk(start: date, end: date, label: str) -> str:
    """Kick off a bulk operation, wait for completion, return download URL."""
    cancel_running_bulk()
    query_body = ORDER_QUERY_TPL.format(start=start.isoformat(), end=end.isoformat())
    resp = gql({"query": BULK_QUERY, "variables": {"query": query_body}})
    errs = (resp.get("data", {}).get("bulkOperationRunQuery") or {}).get("userErrors", [])
    if errs:
        raise RuntimeError(f"Bulk start errors: {errs}")

    print(f"  [{label}] Bulk job started. Waiting …", end="", flush=True)
    while True:
        time.sleep(5)
        data = gql({"query": STATUS_QUERY})
        op   = (data.get("data") or {}).get("currentBulkOperation") or {}
        status = op.get("status", "UNKNOWN")
        count  = op.get("objectCount", "?")
        print(f"\r  [{label}] {status} — {count} objects …     ", end="", flush=True)
        if status == "COMPLETED":
            print()
            return op["url"]
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError(f"Bulk op {status}: {op.get('errorCode')}")


def download_jsonl(url: str) -> list:
    """Stream-download the JSONL result file."""
    r = requests.get(url, stream=True)
    r.raise_for_status()
    lines = []
    for raw in r.iter_lines():
        if raw:
            lines.append(json.loads(raw))
    return lines


# ── AGGREGATION ───────────────────────────────────────────────────────────────

def aggregate(lines: list) -> dict:
    """
    JSONL has two record types:
      - order node  (has 'createdAt')
      - lineItem    (has '__parentId' pointing to the order)
    We join them to get per-product daily sales.
    """
    order_dates = {}   # order_gid → date_str
    products    = defaultdict(lambda: {
        "title": "", "sku": "",
        "by_date": defaultdict(lambda: [0, 0.0]),
    })

    for obj in lines:
        if "createdAt" in obj:
            # Order record
            order_dates[obj["id"]] = obj["createdAt"][:10]
        elif "__parentId" in obj:
            # Line item record
            parent_id = obj["__parentId"]
            day = order_dates.get(parent_id)
            if not day:
                continue
            prod    = obj.get("product") or {}
            variant = obj.get("variant") or {}
            pid     = prod.get("legacyResourceId") or prod.get("id") or obj.get("id", "unknown")
            qty     = int(obj.get("quantity", 0))
            price   = float((obj.get("originalUnitPriceSet") or {}).get("shopMoney", {}).get("amount", 0))
            rev     = qty * price
            p       = products[str(pid)]
            if not p["title"]:
                p["title"] = prod.get("title") or obj.get("title", str(pid))
                p["sku"]   = variant.get("sku", "")
            p["by_date"][day][0] += qty
            p["by_date"][day][1] += rev

    return products


def span_totals(by_date, start: date, end: date):
    u, r = 0, 0.0
    d = start
    while d <= end:
        row = by_date.get(d.isoformat(), [0, 0.0])
        u += row[0]; r += row[1]
        d += timedelta(days=1)
    return u, r


def daily_avg(by_date, start: date, end: date):
    u, r = span_totals(by_date, start, end)
    days = (end - start).days + 1
    return (u / days, r / days) if days > 0 else (0, 0.0)


def uplift(val, avg):
    if avg == 0:
        return None if val == 0 else float("inf")
    return (val / avg - 1) * 100


def fmt(pct):
    if pct is None:      return "–"
    if math.isinf(pct):  return "NA"
    if abs(pct) < 0.5:   return "0%"   # effectively no change
    rounded = round(pct) if abs(pct) >= 10 else round(pct, 1)
    return f"{'+'if pct>0 else ''}{rounded}%"


def fmt_units(x):
    return int(round(x))


def fmt_rev(x):
    return int(round(x))


def parse_pct(val):
    if val in ("NA", "–", "", "0%"): return None
    try: return float(val.replace("%","").replace("+",""))
    except: return None


def consensus_flag(v7, v15, vm, vq):
    vals = [v for v in [v7, v15, vm, vq] if v is not None]
    if not vals: return ""
    pos = sum(1 for v in vals if v > 0)
    neg = sum(1 for v in vals if v < 0)
    if v7 and v7 > 100 and all(v > 0 for v in vals): return "🔥"
    if v7 and v7 > 50  and pos < len(vals):           return "⚡"
    if pos >= 3: return "📈"
    if neg >= 3: return "📉"
    return ""


# ── SHEETS ────────────────────────────────────────────────────────────────────

def connect_sheet():
    sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if sa_json:
        import io
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


def get_ws(sh, name):
    try:    return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=3000, cols=25)


def write_ws(ws, header, rows):
    ws.clear()
    ws.update(range_name="A1", values=[header] + rows, value_input_option="RAW")
    ws.format("1:1", {
        "textFormat":      {"bold": True, "foregroundColor": {"red":1,"green":1,"blue":1}},
        "backgroundColor": {"red": 0.13, "green": 0.13, "blue": 0.13},
    })
    ws.freeze(rows=1)
    print(f"  ✓ '{ws.title}' written — {len(rows)} products.")


# ── PHASE 1 — OUTLIERS DASHBOARD ─────────────────────────────────────────────

def phase1(sh, today):
    yesterday = today - timedelta(days=1)

    # Rolling windows (today excluded from all comparisons)
    # Current windows: [today - N, yesterday]
    # Prior windows:   [today - 2N, today - N - 1]
    c7_start  = today - timedelta(days=7)
    c7_end    = yesterday
    p7_start  = today - timedelta(days=14)
    p7_end    = today - timedelta(days=8)

    c15_start = today - timedelta(days=15)
    c15_end   = yesterday
    p15_start = today - timedelta(days=30)
    p15_end   = today - timedelta(days=16)

    cm_start  = today - timedelta(days=30)
    cm_end    = yesterday
    pm_start  = today - timedelta(days=60)
    pm_end    = today - timedelta(days=31)

    cq_start  = today - timedelta(days=91)
    cq_end    = yesterday
    pq_start  = today - timedelta(days=182)
    pq_end    = today - timedelta(days=92)

    # Fetch must cover the furthest lookback (182 days) plus today for "Units Sold Today"
    fetch_start = pq_start

    print(f"\n[Phase 1] {fetch_start} → {today}  (Outliers Dashboard)")
    url   = run_bulk(fetch_start, today, "P1")
    print(f"  Downloading …")
    lines = download_jsonl(url)
    print(f"  {len(lines)} records downloaded.")
    products = aggregate(lines)

    header = [
        "Product", "SKU", "Flag",
        "Units - Last 7D", "Revenue - Last 7D (INR)", "7D vs Prior 7D (Units %)", "7D vs Prior 7D (Rev %)",
        "Units - Last 15D", "Revenue - Last 15D (INR)", "15D vs Prior 15D (Units %)", "15D vs Prior 15D (Rev %)",
        "Units - Last 1M", "Revenue - Last 1M (INR)", "1M vs Prior 1M (Units %)", "1M vs Prior 1M (Rev %)",
        "Units - Last 1Q", "Revenue - Last 1Q (INR)", "1Q vs Prior 1Q (Units %)", "1Q vs Prior 1Q (Rev %)",
    ]
    rows = []
    for pid, p in products.items():
        bd = p["by_date"]
        tu, tr = bd.get(today.isoformat(), [0, 0.0])

        # Only show products that sold today
        if tu == 0:
            continue

        # Current windows (excluding today)
        c7u,  c7r  = span_totals(bd, c7_start,  c7_end)
        c15u, c15r = span_totals(bd, c15_start, c15_end)
        cmu,  cmr  = span_totals(bd, cm_start,  cm_end)
        cqu,  cqr  = span_totals(bd, cq_start,  cq_end)

        # Prior windows
        p7u,  p7r  = span_totals(bd, p7_start,  p7_end)
        p15u, p15r = span_totals(bd, p15_start, p15_end)
        pmu,  pmr  = span_totals(bd, pm_start,  pm_end)
        pqu,  pqr  = span_totals(bd, pq_start,  pq_end)

        s7  = fmt(uplift(c7u,  p7u))
        s15 = fmt(uplift(c15u, p15u))
        sm  = fmt(uplift(cmu,  pmu))
        sq  = fmt(uplift(cqu,  pqu))
        rows.append([
            p["title"], p["sku"],
            consensus_flag(parse_pct(s7), parse_pct(s15), parse_pct(sm), parse_pct(sq)),
            fmt_units(c7u),  fmt_rev(c7r),  s7,  fmt(uplift(c7r,  p7r)),
            fmt_units(c15u), fmt_rev(c15r), s15, fmt(uplift(c15r, p15r)),
            fmt_units(cmu),  fmt_rev(cmr),  sm,  fmt(uplift(cmr,  pmr)),
            fmt_units(cqu),  fmt_rev(cqr),  sq,  fmt(uplift(cqr,  pqr)),
        ])

    def sort_key(r):
        v = r[5]  # 7D vs Prior 7D (Units %)
        if v in ("–", "NA"): return -9999
        try: return float(v.replace("%","").replace("+",""))
        except: return 0

    rows.sort(key=sort_key, reverse=True)
    write_ws(get_ws(sh, "Outliers Dashboard"), header, rows)


# ── PHASE 2 — YOY COMPARISON ─────────────────────────────────────────────────

def phase2(sh, today):
    mo  = today.replace(day=1)
    qm  = ((today.month - 1) // 3) * 3 + 1
    qtr = today.replace(month=qm, day=1)

    ly_mo_start  = mo.replace(year=today.year - 1)
    ly_mo_end    = ly_mo_start.replace(day=today.day)
    ly_qtr_start = qtr.replace(year=today.year - 1)
    ly_qtr_end   = ly_qtr_start + timedelta(days=(today - qtr).days)

    print(f"\n[Phase 2] {ly_qtr_start} → {ly_qtr_end}  (YoY — same period last year)")
    url_ly   = run_bulk(ly_qtr_start, ly_qtr_end, "P2-LY")
    print("  Downloading LY …")
    lines_ly = download_jsonl(url_ly)
    prod_ly  = aggregate(lines_ly)

    print(f"\n[Phase 2] {qtr} → {today}  (YoY — current period)")
    url_now   = run_bulk(qtr, today, "P2-Now")
    print("  Downloading current …")
    lines_now = download_jsonl(url_now)
    prod_now  = aggregate(lines_now)

    all_pids = set(prod_ly) | set(prod_now)

    qn = (qm - 1) // 3 + 1
    header = [
        "Product", "SKU", "Flag",
        f"Units - {mo.strftime('%b %Y')}", f"Revenue - {mo.strftime('%b %Y')} (INR)",
        f"Units - {ly_mo_start.strftime('%b %Y')} (LY)", f"Revenue - {ly_mo_start.strftime('%b %Y')} (LY) (INR)",
        "Month YoY Uplift (Units %)", "Month YoY Uplift (Rev %)",
        f"Units - Q{qn} {today.year}", f"Revenue - Q{qn} {today.year} (INR)",
        f"Units - Q{qn} {today.year-1} (LY)", f"Revenue - Q{qn} {today.year-1} (LY) (INR)",
        "Quarter YoY Uplift (Units %)", "Quarter YoY Uplift (Rev %)",
    ]
    rows = []
    for pid in all_pids:
        now_bd = (prod_now.get(pid) or {}).get("by_date", {})
        ly_bd  = (prod_ly.get(pid)  or {}).get("by_date", {})
        title  = ((prod_now.get(pid) or prod_ly.get(pid)) or {}).get("title", pid)
        sku    = ((prod_now.get(pid) or prod_ly.get(pid)) or {}).get("sku", "")

        tmu, tmr = span_totals(now_bd, mo,           today)
        lmu, lmr = span_totals(ly_bd,  ly_mo_start,  ly_mo_end)
        tqu, tqr = span_totals(now_bd, qtr,          today)
        lqu, lqr = span_totals(ly_bd,  ly_qtr_start, ly_qtr_end)

        if tmu + lmu + tqu + lqu == 0:
            continue

        sm = fmt(uplift(tmu, lmu))
        sq = fmt(uplift(tqu, lqu))
        vm, vq = parse_pct(sm), parse_pct(sq)
        if vm is not None and vq is not None and vm > 100 and vq > 0:
            flag = "🔥"
        elif vm is not None and vm > 50 and (vq is None or vq <= 0):
            flag = "⚡"
        elif vm is not None and vq is not None and vm > 0 and vq > 0:
            flag = "📈"
        elif vm is not None and vq is not None and vm < 0 and vq < 0:
            flag = "📉"
        else:
            flag = ""

        rows.append([
            title, sku, flag,
            fmt_units(tmu), fmt_rev(tmr),
            fmt_units(lmu), fmt_rev(lmr), sm, fmt(uplift(tmr,lmr)),
            fmt_units(tqu), fmt_rev(tqr),
            fmt_units(lqu), fmt_rev(lqr), sq, fmt(uplift(tqr,lqr)),
        ])

    rows.sort(key=lambda r: r[3], reverse=True)
    write_ws(get_ws(sh, "YoY Comparison"), header, rows)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    global HEADERS
    today = date.today()
    print(f"Swiss Beauty Sales Outliers  |  {today}  (Bulk Operations mode)")
    print("  Fetching fresh Shopify token ...")
    token = get_shopify_token()
    HEADERS = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    sh = connect_sheet()
    print("  ✓ Sheet connected.")
    phase1(sh, today)
    phase2(sh, today)
    print(f"\nAll done! → https://docs.google.com/spreadsheets/d/{SHEET_ID}")

if __name__ == "__main__":
    main()
