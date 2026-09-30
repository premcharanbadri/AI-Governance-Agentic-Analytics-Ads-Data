select
    id                                                  as shop_customer_id,
    lower(trim(email))                                  as email_normalized,
    cast(created_at as timestamp)                       as created_ts_utc
from {{ source('raw', 'shop_customers') }}
