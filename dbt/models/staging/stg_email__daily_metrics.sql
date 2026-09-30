select
    cast(date as date)                                  as report_date,
    email_campaign_id,
    cast(delivered as bigint)                           as delivered,
    cast(opens as bigint)                               as opens,
    cast(clicks as bigint)                              as clicks,
    cast(unsubscribes as bigint)                        as unsubscribes
from {{ source('raw', 'email_daily_metrics') }}
