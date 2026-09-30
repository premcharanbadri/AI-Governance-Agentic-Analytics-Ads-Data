select
    order_id,
    product_id,
    quantity,
    unit_price
from {{ source('raw', 'shop_order_items') }}
