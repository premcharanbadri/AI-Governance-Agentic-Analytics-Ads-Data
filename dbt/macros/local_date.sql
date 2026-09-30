{# Converts a UTC timestamp (stored as a naive TIMESTAMP) to a date in the reporting time zone (decision 0007). #}
{% macro local_date(utc_timestamp) -%}
    cast(({{ utc_timestamp }} at time zone 'UTC') at time zone '{{ var("reporting_timezone") }}' as date)
{%- endmacro %}
