-- Meta calls these "ad sets"; Google calls the same level "ad groups".
select
    cast(id as varchar)             as ad_group_id,
    cast(campaign_id as varchar)    as campaign_id,
    name                            as ad_group_name,
    audience_segment                as target_segment,
    optimization_goal,
    attribution_setting             -- a current snapshot: it does not describe how history was reported
from {{ source('raw', 'meta_adsets') }}
