select
    cast(product_id as bigint)                          as product_id,
    sku,
    product_name,
    category,
    cast(list_price as double)                          as list_price_usd,
    cast(unit_cost as double)                           as unit_cost_usd   -- Finance-only (restricted column)
from {{ source('raw', 'catalog_products') }}
