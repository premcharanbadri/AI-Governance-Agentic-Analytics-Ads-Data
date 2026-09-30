# 03. Domain model (business entities)

Platform-neutral view of Lumen Goods' data. Each platform names these differently
(Google "ad group" = Meta "ad set"); the dbt staging layer maps both onto this model.
All entities are generated as of Pass 2. `WebSession` is derived from web events (sessionized).

```mermaid
classDiagram
    class Campaign {
        campaign_id
        platform
        channel
        intent
        daily_budget
        managed_by
        start_date
        end_date
    }
    class AdGroup {
        ad_group_id
        target_segment
    }
    class Ad {
        ad_id
        format
    }
    class Keyword {
        keyword_id
        keyword_text
        match_type
    }
    class Product {
        product_id
        category
        list_price
        unit_cost  «Finance only»
    }
    class Customer {
        shop_customer_id
        crm_id
        email  «PII»
        marketing_opt_in
    }
    class EmailCampaign {
        email_campaign_id
        target_segment
        recipients
    }
    class BudgetPlan {
        month
        channel
        planned_spend
        version
    }
    class AdPerformanceDaily {
        date
        impressions
        clicks
        spend
    }
    class WebSession {
        session_id
        click_id
        utm_campaign
    }
    class Order {
        order_id
        order_ts
        total
    }
    class OrderItem {
        quantity
        unit_price
    }
    class Refund {
        refund_ts
        amount
    }

    Campaign "1" --> "1..*" AdGroup : contains
    AdGroup "1" --> "1..*" Ad : contains
    AdGroup "1" --> "0..*" Keyword : targets
    Ad "1" --> "0..*" AdPerformanceDaily : reports
    Ad "1" --> "0..*" WebSession : click lands in
    WebSession "1" --> "0..1" Order : converts to
    Customer "1" --> "0..*" Order : places
    Order "1" --> "1..*" OrderItem : contains
    OrderItem "*" --> "1" Product : for
    Order "1" --> "0..*" Refund : may have
    Campaign "*" ..> "1" BudgetPlan : planned under
```
