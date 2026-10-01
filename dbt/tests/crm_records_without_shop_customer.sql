-- Every CRM record should belong to a shop customer; a non-empty result means the normalization is losing matches.
select c.crm_id
from {{ ref('stg_crm__customers') }} c
left join {{ ref('stg_shop__customers') }} s on s.email_normalized = c.email_normalized
where s.shop_customer_id is null
