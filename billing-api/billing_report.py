#!/usr/bin/env python3
"""Pull a Temporal Cloud Billing API report and roll up cost by Namespace and tag.

Flow: CreateBillingReport -> poll GetBillingReport until GENERATED -> download the CSV
-> sum ContractedCost by Namespace, by a tag key (team, env, ...), and by charge.

Needs an API key whose principal can call the Billing API: Account Owner, Finance Admin, or
a Custom Role with cloud.billingreport.create and cloud.billingreport.get. Global Admin can't.

    export TEMPORAL_CLOUD_API_KEY=...            # or --api-key-file
    python3 billing_report.py --start 2026-08 --end 2026-10 --granularity daily --tag team
    python3 billing_report.py --rollup-only report.csv --tag team    # re-run the roll-up

Standard library only. Docs: https://docs.temporal.io/cloud/billing-api
"""
import argparse, csv, json, os, sys, time, uuid
import urllib.error, urllib.request
from collections import defaultdict
from datetime import date

API = "https://saas-api.tmprl.cloud"
API_VERSION = "v0.23.0"  # temporal-cloud-api-version header; granularity needs v0.16.0+
GRANULARITY = {"hourly": "BILLING_REPORT_GRANULARITY_HOURLY",
               "daily": "BILLING_REPORT_GRANULARITY_DAILY",
               "monthly": "BILLING_REPORT_GRANULARITY_MONTHLY"}
UNTAGGED, ACCOUNT_LEVEL = "(untagged)", "(account-level)"


def month_start(ym):
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, 1)


def next_month(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def call(method, path, key, version, body=None):
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "temporal-cloud-api-version": version,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        hint = ""
        if e.code == 401:
            hint = "\nThe API key is invalid, expired, or disabled."
        elif e.code == 403:
            hint = ("\nThe key's principal needs Account Owner, Finance Admin, or a Custom Role with "
                    "cloud.billingreport.create and cloud.billingreport.get. Global Admin is not enough.")
        sys.exit(f"{method} {path} -> HTTP {e.code}: {detail}{hint}")


def pick(d, *names):
    """Field from a JSON response, accepting camelCase or snake_case."""
    for n in names:
        if n in d:
            return d[n]
    return None


def create_report(key, version, start, end_excl, granularity, op_id):
    body = {"spec": {"startTimeInclusive": f"{start}T00:00:00Z",
                     "endTimeExclusive": f"{end_excl}T00:00:00Z",
                     "granularity": GRANULARITY[granularity],
                     "downloadUrlExpirationDuration": "3600s",
                     "description": f"cost roll-up {start} to {end_excl} ({granularity})"},
            "asyncOperationId": op_id}
    # One retry on a dropped connection; the same asyncOperationId makes it idempotent.
    for attempt in (1, 2):
        try:
            resp = call("POST", "/cloud/billing-reports", key, version, body)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                sys.exit(f"CreateBillingReport failed: {e}")
            time.sleep(5)
    rid = pick(resp, "billingReportId", "billing_report_id")
    if not rid:
        sys.exit(f"No billing report id in response: {resp}")
    return rid


def wait_for_report(key, version, rid, timeout_s):
    delay, waited = 5, 0
    while True:
        rep = pick(call("GET", f"/cloud/billing-reports/{rid}", key, version),
                   "billingReport", "billing_report") or {}
        state = str(rep.get("state", ""))
        if state in ("BILLING_REPORT_STATE_GENERATED", "2"):
            return rep
        if state in ("BILLING_REPORT_STATE_FAILED", "3"):
            sys.exit(f"Report {rid} failed: {rep}")
        if waited >= timeout_s:
            sys.exit(f"Report {rid} still {state or 'pending'} after {timeout_s}s. "
                     f"Re-run with --report-id {rid} to keep waiting.")
        print(f"  {state or 'pending'}; checking again in {delay}s", file=sys.stderr)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)  # exponential backoff, per the docs


def download(rep, out_path):
    """Download every file in the report. Signed URLs: no auth header, and they expire."""
    files = pick(rep, "downloadInfo", "download_info") or []
    if not files:
        sys.exit(f"Report is GENERATED but has no download URLs: {rep}")
    paths = []
    for i, f in enumerate(files):
        path = out_path if len(files) == 1 else out_path.replace(".csv", f".part{i + 1}.csv")
        with urllib.request.urlopen(f["url"], timeout=300) as r, open(path, "wb") as fh:
            fh.write(r.read())
        paths.append(path)
        print(f"  saved {path}", file=sys.stderr)
    return paths


def to_dollars(value, currency):
    """ContractedCost as dollars. The docs list BillingCurrency as 'USD (cents)'."""
    v = float(value or 0)
    return v / 100 if "cent" in (currency or "").lower() else v


def tag_value(tags_json, key):
    """Tags column is JSON like {"team": ["bedrock"], "env": ["prd"]}."""
    if not key:
        return None
    try:
        vals = (json.loads(tags_json) if tags_json else {}).get(key) or []
    except json.JSONDecodeError:
        return UNTAGGED
    if isinstance(vals, str):
        vals = [vals]
    return "+".join(sorted(vals)) if vals else UNTAGGED


def rollup(paths, tag_key):
    by_ns, by_tag, by_charge = defaultdict(float), defaultdict(float), defaultdict(float)
    # Actions (millions) next to cost: included Actions are priced at $0, so cost alone
    # understates whoever happened to use the included allowance.
    actions_ns, actions_tag = defaultdict(float), defaultdict(float)
    units, tag_keys = defaultdict(float), set()
    currencies, rows, total = set(), 0, 0.0
    for p in paths:
        with open(p, newline="") as fh:
            for r in csv.DictReader(fh):
                rows += 1
                currencies.add(r.get("BillingCurrency", ""))
                cost = to_dollars(r.get("ContractedCost"), r.get("BillingCurrency"))
                qty = float(r.get("PricingQuantity") or 0)
                ns = r.get("ResourceName") or ACCOUNT_LEVEL
                charge = r.get("ChargeDescription") or r.get("SKUMeter") or "(unlabeled)"
                is_actions = "action" in (r.get("PricingUnit") or "").lower()
                try:
                    tag_keys.update(json.loads(r.get("Tags") or "{}"))
                except json.JSONDecodeError:
                    pass
                total += cost
                by_ns[ns] += cost
                by_charge[charge] += cost
                if is_actions:
                    actions_ns[ns] += qty
                if tag_key:
                    tv = tag_value(r.get("Tags"), tag_key)
                    by_tag[tv] += cost
                    if is_actions:
                        actions_tag[tv] += qty
                units[(charge, r.get("PricingUnit", ""))] += qty
    return {"rows": rows, "currencies": sorted(c for c in currencies if c), "total": total,
            "by_namespace": by_ns, "by_tag": by_tag, "by_charge": by_charge, "units": units,
            "actions_namespace": actions_ns, "actions_tag": actions_tag, "tag_keys": sorted(tag_keys)}


def show(title, d, total, limit, actions=None):
    print(f"\n{title}")
    keys = set(d) | set(actions or {})
    items = sorted(((k, d.get(k, 0.0)) for k in keys),
                   key=lambda kv: (-kv[1], -(actions or {}).get(kv[0], 0.0)))
    for k, v in items[:limit]:
        line = f"  {k:<48} ${v:>12,.2f}  {v / total * 100 if total else 0:5.1f}%"
        if actions is not None:
            line += f"  {actions.get(k, 0.0):>12,.3f}M Actions"
        print(line)
    if len(items) > limit:
        rest = sum(v for _, v in items[limit:])
        print(f"  {'(' + str(len(items) - limit) + ' more)':<48} ${rest:>12,.2f}")


def write_rollup(path, res, tag_key):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["dimension", "key", "contracted_cost_usd", "actions_millions"])
        for dim, d, acts in (("namespace", res["by_namespace"], res["actions_namespace"]),
                             (f"tag:{tag_key}", res["by_tag"], res["actions_tag"]),
                             ("charge", res["by_charge"], {})):
            for k, v in sorted(d.items(), key=lambda kv: -kv[1]):
                w.writerow([dim, k, f"{v:.2f}", f"{acts[k]:.6f}" if k in acts else ""])
    print(f"\nRoll-up written to {path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", help="first billing month, YYYY-MM")
    ap.add_argument("--end", help="last billing month, YYYY-MM (inclusive; current month allowed)")
    ap.add_argument("--granularity", choices=GRANULARITY, default="daily",
                    help="hourly: this + last month; daily: this + 2 prior; monthly: this + 11 prior")
    ap.add_argument("--tag", default="team", help="tag key to roll up by (default: team; '' to skip)")
    ap.add_argument("--out", default="billing_report.csv", help="where to save the raw CSV")
    ap.add_argument("--rollup-csv", help="also write the roll-up to this CSV")
    ap.add_argument("--api-key-file", help="read the API key from this file instead of $TEMPORAL_CLOUD_API_KEY")
    ap.add_argument("--api-version", default=API_VERSION)
    ap.add_argument("--report-id", help="skip create and resume polling an existing report")
    ap.add_argument("--operation-id", help="idempotency key for create (default: random UUID)")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait for generation")
    ap.add_argument("--rollup-only", nargs="+", metavar="CSV", help="roll up existing CSV file(s); no API calls")
    ap.add_argument("--top", type=int, default=25, help="rows to show per table")
    a = ap.parse_args()

    if a.rollup_only:
        paths = a.rollup_only
    else:
        key = (open(a.api_key_file).read().strip() if a.api_key_file
               else os.environ.get("TEMPORAL_CLOUD_API_KEY", "").strip())
        if not key:
            sys.exit("Set TEMPORAL_CLOUD_API_KEY or pass --api-key-file.")
        rid = a.report_id
        if not rid:
            if not (a.start and a.end):
                sys.exit("--start and --end are required (YYYY-MM).")
            start, end_excl = month_start(a.start), next_month(month_start(a.end))
            op_id = a.operation_id or str(uuid.uuid4())
            rid = create_report(key, a.api_version, start, end_excl, a.granularity, op_id)
            print(f"Created billing report {rid} (asyncOperationId {op_id})", file=sys.stderr)
        rep = wait_for_report(key, a.api_version, rid, a.timeout)
        paths = download(rep, a.out)

    res = rollup(paths, a.tag)
    print(f"{res['rows']:,} rows; BillingCurrency {res['currencies'] or ['(blank)']}; "
          f"total ContractedCost ${res['total']:,.2f}")
    print(f"Tag keys in this report: {', '.join(res['tag_keys']) or '(none)'}")
    show("By Namespace (ResourceName)", res["by_namespace"], res["total"], a.top, res["actions_namespace"])
    if a.tag:
        show(f"By tag '{a.tag}'", res["by_tag"], res["total"], a.top, res["actions_tag"])
    show("By charge (ChargeDescription)", res["by_charge"], res["total"], a.top)
    print("\nUnits by charge")
    for (charge, unit), q in sorted(res["units"].items(), key=lambda kv: -kv[1])[:a.top]:
        print(f"  {charge:<48} {q:>16,.2f} {unit}")
    if a.rollup_csv:
        write_rollup(a.rollup_csv, res, a.tag)


if __name__ == "__main__":
    main()
