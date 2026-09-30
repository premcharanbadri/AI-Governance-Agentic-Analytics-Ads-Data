-- Meta-style campaigns. Budgets are cents stored as text; timestamps arrive with an offset and are
-- already converted to UTC by the loader.
select
    cast(id as varchar)                                 as campaign_id,
    name                                                as campaign_name,
    'PAID_SOCIAL'                                       as channel,
    objective,
    status,
    cast(start_time as timestamp)                       as start_ts_utc,
    cast(stop_time as timestamp)                        as planned_end_ts_utc,
    cast(daily_budget as bigint) / 100.0                as daily_budget_usd,
    managed_by
from {{ source('raw', 'meta_campaigns') }}
