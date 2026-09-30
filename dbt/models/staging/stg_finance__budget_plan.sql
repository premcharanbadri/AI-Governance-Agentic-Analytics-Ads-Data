-- Finance revises plans: keep the latest version per month and channel. Finance's channel labels
-- ("SEM", "Paid Social - Meta") are mapped to channel codes through the channel_map seed.
with versions as (
    select
        cast(month || '-01' as date)                    as plan_month,
        channel_label,
        cast(planned_spend as double)                   as planned_spend_usd,
        cast(version as integer)                        as plan_version,
        cast(updated_at as date)                        as updated_on,
        row_number() over (partition by month, channel_label order by cast(version as integer) desc) as rn,
        count(*)     over (partition by month, channel_label)                                         as n_versions
    from {{ source('raw', 'finance_budget_plan') }}
)
select
    v.plan_month, m.channel, v.channel_label, v.planned_spend_usd, v.plan_version, v.updated_on,
    v.n_versions > 1 as was_revised
from versions v
left join {{ ref('channel_map') }} m on m.channel_label = v.channel_label
where v.rn = 1
