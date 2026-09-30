select
    cast(date as date)                                  as report_date,
    cast(campaign_id as varchar)                        as campaign_id,
    cast(impression_share as double)                    as impression_share,
    cast(overlap_rate as double)                        as overlap_rate,
    cast(outranking_share as double)                    as outranking_share
from {{ source('raw', 'google_auction_insights_daily') }}
