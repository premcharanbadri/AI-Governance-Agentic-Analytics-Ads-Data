-- Fails if fewer than 95% of shop customers match a CRM record: identity resolution has probably broken.
select count(*) as customers, avg(case when has_crm_record then 1.0 else 0.0 end) as match_rate
from {{ ref('int_customers_resolved') }}
having avg(case when has_crm_record then 1.0 else 0.0 end) < 0.95
