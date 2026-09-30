{{ config(materialized='table') }}
-- One row per ad per day, using the LATEST extract. Meta's first extract for a day understates conversions; the
-- restated final arrives three days later. The last three days therefore only have a provisional version.
-- From 2026-09-03 Meta returns `amount_spent` instead of `spend`; both spellings are handled here and the
-- singular test meta_schema_drift_detected warns when the new one appears.
with parsed as (
    select
        cast(date_start as date)                                        as report_date,
        'America/New_York'                                              as account_timezone,
        cast(campaign_id as varchar)                                    as campaign_id,
        cast(adset_id as varchar)                                       as ad_group_id,
        cast(ad_id as varchar)                                          as ad_id,
        cast(impressions as bigint)                                     as impressions,
        cast(link_clicks as bigint)                                     as clicks,
        cast(coalesce(spend, amount_spent) as double)                   as spend_usd,
        (spend is null and amount_spent is not null)                    as used_renamed_field,
        try_cast(list_extract(list_filter(actions, a -> a.action_type = 'purchase'), 1).value as bigint)
                                                                        as platform_conversions,
        try_cast(list_extract(list_filter(action_values, a -> a.action_type = 'purchase'), 1).value as double)
                                                                        as platform_conversion_value,
        cast(extracted_at as timestamp)                                 as extracted_at,
        filename                                                        as source_file,
        filename like '%insights_restated%'                             as is_restated_extract
    from {{ source('raw', 'meta_insights') }}
),
ranked as (
    select
        *,
        row_number() over (partition by report_date, ad_id order by extracted_at desc) as version_rank,
        count(*)     over (partition by report_date, ad_id)                            as n_versions
    from parsed
)
select
    report_date, account_timezone, campaign_id, ad_group_id, ad_id, impressions, clicks, spend_usd,
    platform_conversions, platform_conversion_value, extracted_at, used_renamed_field,
    not is_restated_extract as is_provisional,
    n_versions
from ranked
where version_rank = 1
