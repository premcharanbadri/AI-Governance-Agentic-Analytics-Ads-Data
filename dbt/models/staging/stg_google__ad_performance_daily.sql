-- One row per ad per day. `report_date` is the Google account's date (Pacific time) and cannot be
-- re-bucketed to another zone: the source only delivers daily totals (decision 0007).
select
    cast(date as date)                                  as report_date,
    'America/Los_Angeles'                               as account_timezone,
    cast(campaign_id as varchar)                        as campaign_id,
    cast(ad_group_id as varchar)                        as ad_group_id,
    cast(ad_id as varchar)                              as ad_id,
    cast(impressions as bigint)                         as impressions,
    cast(clicks as bigint)                              as clicks,
    cast(cost_micros as bigint) / 1000000.0             as cost_usd,
    cast(platform_conversions as double)                as platform_conversions,
    cast(platform_conversion_value as double)           as platform_conversion_value
from {{ source('raw', 'google_ad_perf_daily') }}
