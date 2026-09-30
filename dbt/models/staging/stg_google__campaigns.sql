-- Google-style campaigns. Money is stored in micros (millionths of a dollar).
select
    cast(campaign_id as varchar)                        as campaign_id,
    campaign_name,
    channel_type                                        as channel,
    status,
    cast(start_date as date)                            as start_date,
    try_cast(nullif(end_date, '') as date)              as planned_end_date,
    cast(daily_budget_micros as bigint) / 1000000.0     as daily_budget_usd,
    managed_by
from {{ source('raw', 'google_campaigns') }}
