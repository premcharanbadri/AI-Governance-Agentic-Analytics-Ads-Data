-- Email has platform fees but no media spend, which is why "email ROAS" is ambiguous.
select
    cast(month || '-01' as date)                        as fee_month,
    cast(fee_amount as double)                          as fee_usd
from {{ source('raw', 'email_platform_fees') }}
