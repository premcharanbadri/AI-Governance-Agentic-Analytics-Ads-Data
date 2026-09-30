# 04. Generator Pass 1: activity

Pass 2 (history.py) then simulates spend, clicks, orders, refunds and web events from this reference data, including
the July CPC spike and the planted orders (check_history.py, 39 checks). Pass 3 (incidents.py) damages the raw files:
tracking outage, duplicate and late events, Meta restatements and a field rename (check_incidents.py, 26 checks).

```mermaid
flowchart TD
    START([Start]) --> CFG[Load config.yaml<br/>seed + optional --scale]
    CFG --> PROD[1. Products<br/>log-scale prices, unit cost]
    PROD --> CAMP[2. Campaign master<br/>evergreen, flights, seasonal]
    CAMP --> PLANT[Plant: test, tiny,<br/>injected-name campaigns]
    PLANT --> PAUSE[Pause 4 large campaigns<br/>on 2026-08-04]
    PAUSE --> BUD[Solve budgets per channel<br/>to hit $1.5M/month]
    BUD --> SPLIT{Platform?}
    SPLIT -- Google --> GOO[3. Google export<br/>micros, Pacific time, keywords]
    SPLIT -- Meta --> MET[4. Meta export<br/>JSON, cents strings, Eastern time]
    GOO --> CUST
    MET --> CUST[5. Customers<br/>legacy base + new sign-ups,<br/>two systems]
    CUST --> QUIRK[Plant: CRM gaps, email<br/>mismatches, small ZIP cell]
    QUIRK --> EMAIL[6. Email sends<br/>recipients from real subscriber counts]
    EMAIL --> PLAN[7. Finance budget plan<br/>noise + revisions]
    PLAN --> WRITE[Write raw/, state/,<br/>ground_truth/]
    WRITE --> CHECK[Run check_reference.py<br/>43 checks]
    CHECK --> OK{All pass?}
    OK -- yes --> DONE([Done])
    OK -- no --> FIX[Fix config or code] --> CFG
```
