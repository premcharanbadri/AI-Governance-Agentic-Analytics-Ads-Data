-- Certified first-party attribution (decision 0006): every order is credited to its LAST TAGGED MARKETING TOUCH
-- (paid click or tagged email link) before purchase, within `attribution_window_days` (default 7).
--
-- How it works: the purchase event gives the visitor's anonymous_id; the same anonymous_id is on the landing event
-- that carried the click ID or UTM tags, possibly days earlier in a different session. Direct visits are not touches,
-- so they never override an earlier one. The latest touch up to `attribution_max_lookback_days` is kept with its lag,
-- so any shorter window can be applied without rebuilding; `is_attributed` applies the certified window.
--
-- Orders with no attribution are kept, never dropped, and say why in `attribution_status`.
{{ config(materialized='table') }}
{% set window_days = var('attribution_window_days') %}
{% set lookback_days = var('attribution_max_lookback_days') %}

with orders as (
    select order_id, shop_customer_id, session_id, order_ts_utc
    from {{ ref('stg_shop__orders') }}
),

purchases as (      -- links an order to a visitor
    select session_id, anonymous_id
    from {{ ref('stg_web__events') }}
    where event_type = 'purchase'
),

touches as (        -- landings that carried a click ID or a tagged email link
    select
        anonymous_id,
        event_ts_utc                                            as touch_ts_utc,
        utm_campaign                                            as campaign_id,
        utm_content                                             as ad_id,
        case when gclid is not null then 'google'
             when fbclid is not null then 'meta'
             else 'email' end                                   as platform,
        case when gclid is not null or fbclid is not null then 'paid_click' else 'email_link' end as touch_type
    from {{ ref('stg_web__events') }}
    where event_type = 'landing'
      and (gclid is not null or fbclid is not null or utm_medium = 'email')
),

linked as (
    select o.order_id, o.order_ts_utc, o.session_id, p.anonymous_id
    from orders o
    left join purchases p on p.session_id = o.session_id
),

last_touch as (
    select
        l.order_id,
        t.touch_type, t.platform, t.campaign_id, t.ad_id, t.touch_ts_utc,
        date_diff('second', t.touch_ts_utc, l.order_ts_utc) / 86400.0 as lag_days,
        row_number() over (partition by l.order_id order by t.touch_ts_utc desc) as touch_rank
    from linked l
    join touches t
      on t.anonymous_id = l.anonymous_id
     and t.touch_ts_utc <= l.order_ts_utc
     and t.touch_ts_utc >= l.order_ts_utc - interval {{ lookback_days }} day
)

select
    o.order_id,
    o.shop_customer_id,
    o.order_ts_utc,
    l.anonymous_id,
    lt.touch_type,
    lt.platform                                                 as candidate_platform,
    lt.campaign_id                                              as candidate_campaign_id,
    lt.ad_id                                                    as candidate_ad_id,
    lt.touch_ts_utc,
    lt.lag_days                                                 as touch_lag_days,
    {{ window_days }}                                           as attribution_window_days,
    coalesce(lt.lag_days <= {{ window_days }}, false)           as is_attributed,
    case when lt.lag_days <= {{ window_days }} then lt.campaign_id end as attributed_campaign_id,
    case when lt.lag_days <= {{ window_days }} then lt.platform end    as attributed_platform,
    case
        when o.session_id is null        then 'untracked'          -- no web session recorded (before tracking began)
        when lt.order_id is null         then 'no_marketing_touch' -- tracked visit, but no click ID or tagged email link
        when lt.lag_days > {{ window_days }} then 'outside_window' -- a touch exists, but too long ago
        else 'attributed'
    end                                                         as attribution_status
from orders o
left join linked l on l.order_id = o.order_id
left join last_touch lt on lt.order_id = o.order_id and lt.touch_rank = 1
