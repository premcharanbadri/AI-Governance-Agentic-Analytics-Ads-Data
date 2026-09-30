select
    refund_id,
    order_id,
    cast(refund_ts as timestamp)                        as refund_ts_utc,
    amount                                              as refund_amount,
    reason
from {{ source('raw', 'shop_refunds') }}
