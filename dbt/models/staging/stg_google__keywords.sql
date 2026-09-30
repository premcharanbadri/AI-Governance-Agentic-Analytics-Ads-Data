select
    cast(keyword_id as varchar)     as keyword_id,
    cast(ad_group_id as varchar)    as ad_group_id,
    keyword_text,
    match_type
from {{ source('raw', 'google_keywords') }}
