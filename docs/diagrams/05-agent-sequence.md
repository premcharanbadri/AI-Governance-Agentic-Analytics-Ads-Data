# 05. Level 3 agent: answering one question (planned)

Planned design, not yet built. Shows the permission check happening in infrastructure
before any data is touched, and the audit trail for both allowed and denied requests.

```mermaid
sequenceDiagram
    actor U as Analyst
    participant A as Agent
    participant G as Policy gateway
    participant M as MCP server
    participant S as Semantic layer
    participant W as Snowflake (ANALYST role)
    participant L as Audit log

    U->>A: "What was ROAS by channel last month?"
    A->>G: call resolve_metric("ROAS")
    G->>G: check role + tool permission
    G->>M: allowed
    M->>S: look up certified definition
    S-->>A: revenue / spend, net of returns, 7-day click
    A->>G: call query_certified_metric(ROAS, channel, Aug 2026)
    G->>M: allowed
    M->>W: run governed query
    W-->>A: results
    G->>L: record both calls
    A-->>U: answer + definition used + sources

    Note over U,L: Denied path
    U->>A: "List emails of Black Friday buyers"
    A->>G: call query(customer emails)
    G->>L: record denied request
    G-->>A: denied: PII not allowed for ANALYST
    A-->>U: explains it can't share contact details
```
