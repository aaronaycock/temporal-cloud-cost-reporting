# Temporal Cloud cost reporting examples

Examples for four stages of cost analysis on Temporal Cloud:

1. **Allocate:** cost by namespace and tag from the Billing API.
2. **Attribute:** Actions by workflow type from the OpenMetrics endpoint.
3. **Estimate:** Actions per run, to size new work before it ships.
4. **Optimize:** an alert when a namespace's daily Actions spike.

This code has been tested in a lab. However, it may need to be modified to match your specific environment.

| Path | What it does |
|---|---|
| `billing-api/billing_report.py` | Creates a Billing API report, waits for it, downloads the CSV, and rolls up cost by namespace, tag, and charge. Python standard library only. |
| `billing-api/billing-report-reader.json` | A custom role that can only create and read billing reports. |
| `promql/rules.yml` | Prometheus recording rules for Actions by namespace and workflow type, plus a spike alert. |
| `promql/queries.md` | Ad hoc Grafana queries: Actions by workflow type, Actions per run, daily totals. |
| `promql/*_test.yml` | promtool unit tests for the rules and queries. |

## 1. Billing API: cost by namespace and tag

The [Billing API](https://docs.temporal.io/cloud/billing-api) needs the Account Owner, a Finance Admin, or a custom role
with `cloud.billingreport.create` and `cloud.billingreport.get`. Global Admin can't call it.

**Least-privilege setup.** The Account Owner creates the custom role and gives it to a Read-Only service account, using
the [Temporal CLI](https://docs.temporal.io/cli/cloud) or **Identities > Custom Roles** in the Cloud UI:

```bash
temporal cloud custom-role create --spec @billing-api/billing-report-reader.json
temporal cloud service-account create --name billing-reports --account-role read
temporal cloud service-account set-custom-roles --service-account-id <service-account-id> --custom-role <role-id>
temporal cloud apikey create-for-service-account --service-account-id <service-account-id> \
  --display-name billing-reports --expiry-duration 720h   # 24h minimum
```

**Pull a report:**

```bash
export TEMPORAL_CLOUD_API_KEY=...        # or --api-key-file path/to/key
python3 billing-api/billing_report.py --start 2026-09 --end 2026-10 --granularity daily --tag team
python3 billing-api/billing_report.py --rollup-only billing_report.csv --tag env   # re-run the roll-up, no API calls
```

What to expect in the report:

- Amounts are in cents (`BillingCurrency` is `USD (cents)`). The script converts them.
- The plan fee arrives as account-level rows with no namespace or tag, prorated daily.
- Included Actions show as $0 rows, so compare Actions as well as dollars.
- Tags are read when the report runs, so tagging a namespace today re-attributes its past months too
  ([cost governance guide](https://docs.temporal.io/best-practices/cost-governance)).
- One report generates at a time per account.

## 2. Actions by workflow type

`temporal_cloud_v1_billable_action_count` splits each namespace's Actions by workflow type and Action type, close to
real time. Scrape it with a service account that has the Metrics Read-Only role.

- Values are per-second rates, one sample per minute, about 3 minutes behind, and idle series aren't emitted. Count
  Actions with `sum_over_time(...) * 60` on raw samples, not subqueries.
- Scrape every 30s with `honor_timestamps: true`.
- `rules.yml` records hourly Actions and workflow starts by workflow type. Load it with `rule_files`.

Share of a namespace's Actions by workflow type over 7 days:

```promql
sum_over_time(namespace_workflow_type:actions:1h[7d])
/ on (temporal_namespace) group_left
sum by (temporal_namespace) (sum_over_time(namespace_workflow_type:actions:1h[7d]))
```

`promql/queries.md` has the same views without recording rules, for a quick check in Grafana.

## 3. Estimating new work

```promql
sum_over_time(namespace_workflow_type:actions:1h[24h])
/ sum_over_time(namespace_workflow_type:starts:1h[24h])
```

Then: runs × Actions per run × your rate per 1M Actions. This works for workflows that start and finish within the day;
long-running workflows read much higher, so measure those by Actions per day.

For code that hasn't shipped yet, an agent with the
[temporal-developer skill](https://github.com/temporalio/skill-temporal-developer) can read a workflow, estimate Actions
per run, and flag patterns from the [cost optimization guide](https://docs.temporal.io/best-practices/cost-optimization).
Check its estimate against the measured number once the workflow runs.

## 4. Spike alert

`TemporalActionsSpike` fires when a namespace's last 24 hours is more than 2x its highest 24 hours in the prior 30 days
and at least 1M Actions. Repeated big days (weekly batch runs, for example) raise the 30-day high, so they stop
alerting after the first one. Silence planned load tests in Alertmanager, and tune the 2x and 1M thresholds to your
traffic.

## Tests

```bash
docker run --rm -v "$PWD/promql:/p" -w /p --entrypoint promtool prom/prometheus:v2.53.4 \
  test rules rules_test.yml queries_test.yml
```

## Related

- [Cost governance on Temporal Cloud](https://docs.temporal.io/best-practices/cost-governance)
- [Workflow cost optimization](https://docs.temporal.io/best-practices/cost-optimization)
- [Workflow History Export](https://docs.temporal.io/cloud/export), the
  [export-to-Parquet sample](https://github.com/temporalio/samples-python/tree/main/cloud_export_to_parquet), and
  [Insights from Workflow History Export](https://temporal.io/blog/get-insights-from-workflow-histories-export-on-temporal-cloud)
- [OpenMetrics metrics reference](https://docs.temporal.io/cloud/metrics/openmetrics/metrics-reference)
