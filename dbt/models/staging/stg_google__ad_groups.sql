select
    cast(ad_group_id as varchar)    as ad_group_id,
    cast(campaign_id as varchar)    as campaign_id,
    ad_group_name,
    target_segment
from {{ source('raw', 'google_ad_groups') }}
