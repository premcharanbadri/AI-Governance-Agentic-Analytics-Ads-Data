select
    email_campaign_id,
    name                                                as campaign_name,
    cast(cast(send_ts as timestamptz) at time zone 'UTC' as timestamp) as send_ts_utc,
    target_segment,
    cast(recipients as bigint)                          as recipients
from {{ source('raw', 'email_campaigns') }}
