{{ config(severity='warn') }}
-- Informational: how many CRM records a naive join on the RAW email would have missed (capitalization, trailing space).
select count(*) as crm_records_missed_by_naive_join
from {{ source('raw', 'crm_customers') }} c
left join {{ source('raw', 'shop_customers') }} s on s.email = c.email
where s.id is null
having count(*) > 0
