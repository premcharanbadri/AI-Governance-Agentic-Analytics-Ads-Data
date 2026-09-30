{{ config(severity='warn') }}
-- Informational: warns with the number of retry duplicates removed in staging.
select r.raw_rows - s.staged_rows as duplicates_dropped
from (select count(*) as raw_rows from {{ source('raw', 'web_events') }}) r,
     (select count(*) as staged_rows from {{ ref('stg_web__events') }}) s
where r.raw_rows > s.staged_rows
