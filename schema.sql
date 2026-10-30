-- MPOB single source of truth -- DuckDB schema
-- Layers: bronze (raw files in data/raw) -> silver (obs, append-only) -> gold (views)

CREATE TABLE IF NOT EXISTS ingest_manifest (
    url           VARCHAR,
    family        VARCHAR,
    title         VARCHAR,
    year          INTEGER,
    sha256        VARCHAR,
    bytes         BIGINT,
    fetched_at    TIMESTAMP
);

-- One row per series. agg_rule is the ONLY legal way to roll the series up.
CREATE TABLE IF NOT EXISTS dim_series (
    series_key    VARCHAR PRIMARY KEY,
    dataset       VARCHAR,               -- headline | price | dosm
    measure       VARCHAR,               -- volume | stock | price | index
    product       VARCHAR,
    geography     VARCHAR,
    unit          VARCHAR,
    agg_rule      VARCHAR,               -- sum | avg | last
    native_freq   VARCHAR                -- daily | monthly
);

-- Append-only. Revisions never overwrite: a changed value gets revision + 1.
CREATE TABLE IF NOT EXISTS obs (
    series_key    VARCHAR,
    period_start  DATE,
    period_end    DATE,                  -- inclusive
    granularity   VARCHAR,               -- d | m
    value         DOUBLE,                -- NULL for PH/NT (holiday / no trade) days
    unit          VARCHAR,
    status        VARCHAR,               -- actual | PH | NT | derived
    revision      INTEGER DEFAULT 1,
    coverage      DOUBLE DEFAULT 1.0,
    source_url    VARCHAR,
    released_at   TIMESTAMP,
    ingested_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (series_key, period_start, granularity, revision)
);

CREATE OR REPLACE VIEW v_obs_latest AS
SELECT * EXCLUDE (rn) FROM (
    SELECT *, row_number() OVER (
        PARTITION BY series_key, period_start, granularity ORDER BY revision DESC
    ) AS rn FROM obs
) WHERE rn = 1;

CREATE OR REPLACE VIEW v_daily AS
SELECT o.series_key, o.period_start AS period, o.value, o.status, o.unit
FROM v_obs_latest o WHERE o.granularity = 'd';

-- Weekly (ISO week, Monday start). Aggregates are 'derived'; n_obs = priced days used.
CREATE OR REPLACE VIEW v_weekly AS
SELECT o.series_key, strftime(date_trunc('week', o.period_start), '%Y-%m-%d') AS period,
       CASE s.agg_rule WHEN 'sum' THEN sum(o.value) WHEN 'avg' THEN avg(o.value)
            ELSE arg_max(o.value, o.period_end) END AS value,
       s.unit, 'derived' AS status, count(o.value) AS n_obs
FROM v_obs_latest o JOIN dim_series s USING (series_key)
WHERE o.granularity = 'd' AND o.value IS NOT NULL
GROUP BY o.series_key, date_trunc('week', o.period_start), s.agg_rule, s.unit;

-- Monthly: daily series are rolled up; natively monthly series pass through.
CREATE OR REPLACE VIEW v_monthly AS
SELECT o.series_key, strftime(date_trunc('month', o.period_start), '%Y-%m') AS period,
       CASE s.agg_rule WHEN 'sum' THEN sum(o.value) WHEN 'avg' THEN avg(o.value)
            ELSE arg_max(o.value, o.period_end) END AS value,
       s.unit, 'derived' AS status, count(o.value) AS n_obs
FROM v_obs_latest o JOIN dim_series s USING (series_key)
WHERE o.granularity = 'd' AND o.value IS NOT NULL
GROUP BY o.series_key, date_trunc('month', o.period_start), s.agg_rule, s.unit
UNION ALL
SELECT o.series_key, strftime(o.period_start, '%Y-%m'), o.value, o.unit, o.status, 1
FROM v_obs_latest o WHERE o.granularity = 'm' AND o.value IS NOT NULL;

-- Quarterly ALWAYS rolls up from v_monthly, never from raw daily rows.
-- complete = all three months present.
CREATE OR REPLACE VIEW v_quarterly AS
SELECT m.series_key,
       year(strptime(m.period, '%Y-%m')) || '-Q' || quarter(strptime(m.period, '%Y-%m')) AS period,
       CASE s.agg_rule WHEN 'sum' THEN sum(m.value) WHEN 'avg' THEN avg(m.value)
            ELSE arg_max(m.value, m.period) END AS value,
       s.unit, 'derived' AS status, count(*) AS n_months, count(*) = 3 AS complete
FROM v_monthly m JOIN dim_series s USING (series_key)
GROUP BY m.series_key, year(strptime(m.period, '%Y-%m')), quarter(strptime(m.period, '%Y-%m')),
         s.agg_rule, s.unit;
