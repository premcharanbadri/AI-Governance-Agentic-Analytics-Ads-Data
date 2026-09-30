{{ config(severity='warn') }}
-- Warns (does not fail) when Meta's renamed field is in use. The staging model handles both spellings, but someone
-- should know the API changed.
select
    count(*)            as rows_using_renamed_field,
    min(report_date)    as first_affected_report_date
from {{ ref('stg_meta__insights') }}
where used_renamed_field
having count(*) > 0
