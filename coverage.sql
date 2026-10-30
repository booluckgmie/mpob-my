-- Per-series coverage: how much of each series exists, how much of it is real.
CREATE OR REPLACE VIEW v_coverage AS
SELECT s.series_key, s.dataset, s.native_freq,
       min(o.period_start) AS first_period,
       max(o.period_end)   AS last_period,
       count(*)            AS observations,
       round(100.0 * count(*) FILTER (o.status = 'actual') / count(*), 1) AS pct_actual,
       count(*) FILTER (o.value IS NULL)                                  AS flagged_days,
       max(o.ingested_at)  AS last_ingested
FROM v_obs_latest o JOIN dim_series s USING (series_key)
GROUP BY s.series_key, s.dataset, s.native_freq;
