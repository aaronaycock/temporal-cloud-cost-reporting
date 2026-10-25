# Grafana queries for Actions

Ad hoc queries for Grafana **Explore** against a Prometheus data source that scrapes the
[Temporal Cloud OpenMetrics endpoint](https://docs.temporal.io/cloud/metrics/openmetrics). They need no recording rules.
Replace `<namespace>.<account>` with one of your namespaces and use query type **Instant**.

**How these metrics behave:** values are per-second rates, one sample per minute, about 3 minutes behind, and a series
isn't emitted while its namespace is idle. Sum the raw samples and multiply by 60. Avoid subqueries such as `[24h:1m]`:
Prometheus reuses a series' last sample for up to 5 minutes, so idle series get counted more than once
(`rules_test.yml` shows a 50% overcount).

## 1. Are both Action metrics in Prometheus?

```promql
count by (__name__) ({__name__=~"temporal_cloud_v1_(total|billable)_action_count"})
```

Expect two rows. If `temporal_cloud_v1_billable_action_count` is missing, your scrape filters it out.

## 2. Actions for one namespace over 24 hours

```promql
sum(sum_over_time(temporal_cloud_v1_total_action_count{temporal_namespace="<namespace>.<account>"}[24h])) * 60
```

End the time range at 00:00 UTC to get one UTC day, then compare with the Usage page or a daily Billing API report for
the same day. Drop the namespace filter for the whole account.

## 3. Actions by workflow type

```promql
sort_desc(sum by (temporal_workflow_type) (
  sum_over_time(temporal_cloud_v1_billable_action_count{temporal_namespace="<namespace>.<account>"}[24h])
) * 60)
```

The rows add up to a little less than query 2, because the billable metric leaves out a few Action types.

## 4. Actions per run by workflow type

```promql
  sum by (temporal_workflow_type) (
    sum_over_time(temporal_cloud_v1_billable_action_count{temporal_namespace="<namespace>.<account>"}[24h]))
/ sum by (temporal_workflow_type) (
    sum_over_time(temporal_cloud_v1_billable_action_count{temporal_namespace="<namespace>.<account>", action_type="start_workflow"}[24h]))
```

This is a true per-run number only for workflows that start and finish within the window. Long-running, cron,
scheduled, and continue-as-new workflows read much higher because they start rarely and run all day; read those by
Actions per day instead. If a workflow type shows no result, it may be started another way, so check its `action_type`
values.

To estimate new work: runs × Actions per run × your rate per 1M Actions.

## 5. Daily totals over a week

Use a Time series panel with **Min interval** `1d`. Each point at 00:00 UTC is the day before it.

```promql
sum by (temporal_namespace) (
  sum_over_time(temporal_cloud_v1_total_action_count{temporal_namespace=~"<namespace-1>.<account>|<namespace-2>.<account>"}[24h])
) * 60
```

## If the numbers don't match the Usage page

- **Much lower:** gaps in the scrape, a scrape interval over 60s (it skips samples), or a namespace filter on the scrape.
- **Shifted by hours or a day:** the scrape isn't honoring timestamps (`honor_timestamps: true`). Samples are stamped
  about 3 minutes back.
- **About double:** two scrape jobs collect the same metrics. Add `{job="..."}` to pick one.
- **Higher on a quiet namespace only:** a subquery or roll-up slipped in. Sum raw samples as above.

## Find namespaces that go idle

Busy namespaces never go idle, so raw sums and subqueries agree there. Namespaces with fewer than 1,440 samples a day
go idle at times, and those are where the subquery form overcounts.

```promql
sort(sum by (temporal_namespace) (count_over_time(temporal_cloud_v1_total_action_count[24h])))
```
