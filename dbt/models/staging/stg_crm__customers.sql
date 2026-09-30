-- The CRM has its own IDs, types some emails with different capitalization or a trailing space, formats phone
-- numbers three ways, and stores naive Central-time timestamps (no offset).
select
    crm_id,
    lower(trim(email))                                  as email_normalized,
    regexp_replace(phone, '[^0-9]', '', 'g')            as phone_digits,
    first_name,
    last_name,
    zip,
    state,
    cast((cast(created_at as timestamp) at time zone 'America/Chicago') at time zone 'UTC' as timestamp)
                                                        as created_ts_utc,
    cast(marketing_opt_in as boolean)                   as marketing_opt_in,
    cast((try_cast(nullif(opt_out_ts, '') as timestamp) at time zone 'America/Chicago') at time zone 'UTC' as timestamp)
                                                        as opt_out_ts_utc
from {{ source('raw', 'crm_customers') }}
