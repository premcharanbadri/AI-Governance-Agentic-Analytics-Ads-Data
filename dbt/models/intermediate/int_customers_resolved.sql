-- One row per shop customer, with the CRM record attached when there is one.
--
-- The shop and the CRM use different IDs (shop_cust_... vs CRM-...). They can only be matched on email, and raw
-- emails differ in capitalization or trailing spaces for a couple of percent of CRM records, so the match uses the
-- normalized email built in staging. Guest checkouts never reach the CRM, so some customers have no CRM record.
select
    s.shop_customer_id,
    s.email_normalized,                     -- PII
    s.created_ts_utc                        as shop_created_ts_utc,
    c.crm_id,
    c.zip,                                  -- PII (quasi-identifier)
    c.state,
    c.marketing_opt_in,
    c.opt_out_ts_utc,
    c.crm_id is not null                    as has_crm_record
from {{ ref('stg_shop__customers') }} s
left join {{ ref('stg_crm__customers') }} c
    on c.email_normalized = s.email_normalized
