select
    cast(ad_id as varchar)          as ad_id,
    cast(ad_group_id as varchar)    as ad_group_id,
    ad_name,
    format                          as ad_format
from {{ source('raw', 'google_ads') }}
