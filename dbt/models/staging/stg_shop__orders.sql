select
    order_id,
    shop_customer_id,
    session_id,
    cast(order_ts as timestamp)                         as order_ts_utc,
    subtotal, shipping, discount, tax, total,
    nullif(discount_code, '')                           as discount_code,
    status
from {{ source('raw', 'shop_orders') }}
