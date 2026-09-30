{{ config(materialized='table') }}
-- One row per event. Retries deliver the same event_id more than once; the first arrival is kept.
-- `sample_weight` is 1 for every buying path and 10 for the 10% sample of other sessions (decision 0002):
-- use SUM(sample_weight), never COUNT(*), for sessions and session-based rates.
with ranked as (
    select
        *,
        row_number() over (partition by event_id order by received_ts) as arrival_rank
    from {{ source('raw', 'web_events') }}
)
select
    event_id, event_type, event_ts as event_ts_utc, received_ts as received_ts_utc,
    anonymous_id, session_id, customer_id as shop_customer_id,
    utm_source, utm_medium, utm_campaign, utm_content, gclid, fbclid,
    landing_page, device_type, ip_address, user_agent, sample_weight,
    (received_ts - event_ts) > interval 10 minute as is_late
from ranked
where arrival_rank = 1
