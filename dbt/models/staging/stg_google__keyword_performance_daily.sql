select
    cast(date as date)                                  as report_date,
    'America/Los_Angeles'                               as account_timezone,
    cast(keyword_id as varchar)                         as keyword_id,
    keyword_text,
    match_type,
    cast(impressions as bigint)                         as impressions,
    cast(clicks as bigint)                              as clicks,
    cast(cost_micros as bigint) / 1000000.0             as cost_usd
from {{ source('raw', 'google_keyword_perf_daily') }}
