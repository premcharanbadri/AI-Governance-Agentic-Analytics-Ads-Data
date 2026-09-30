select
    cast(id as varchar)             as ad_id,
    cast(adset_id as varchar)       as ad_group_id,
    name                            as ad_name,
    creative_type
from {{ source('raw', 'meta_ads') }}
