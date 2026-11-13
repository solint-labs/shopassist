# Connecting Shopify

ShopAssist reads from the **Shopify Admin GraphQL API** (default version `2026-10`).

## Create the app

Since 1 January 2026, new custom apps can't be created in the Shopify admin. Create them in the **Dev Dashboard**:

1. Dev Dashboard → *Create app*. Under **Versions**, set the access scopes:
   - `read_orders`: order status and tracking
   - `read_products`: catalog search and product ingestion
   - `read_content` / `read_online_store_pages`: store pages for the knowledge base (optional)
   - Part 3 adds `write_orders` (cancellations) and `write_returns` / `read_returns` (return requests)
2. Configure **protected customer data** access for the fields we use (email, phone, name).
   Order verification compares the customer's email/phone, so this is required.
3. Release the version and install the app on your store.
4. Copy the **Client ID** and **Client secret** into `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET`.

ShopAssist then uses the **client credentials grant** (`POST https://{shop}.myshopify.com/admin/oauth/access_token`
with `grant_type=client_credentials`). Tokens last about 24 hours. The client caches them and refreshes them
five minutes before they expire. This grant only works when the app and the store belong to the same
Shopify organization. If you build for other merchants, use the authorization code grant instead.

If you still have a **legacy custom app** created in the admin before 2026, set `SHOPIFY_ACCESS_TOKEN=shpat_...`.
Those apps keep working.

## Index store content

```bash
python -m scripts.ingest --shopify --query "can I return opened coffee?"
```

This pulls shop policies, pages and active products into Qdrant alongside `knowledge/<tenant>/*.md`.
Run it nightly, or from a `products/update` webhook. Unchanged documents are skipped.

## Rate limits

The Admin GraphQL API uses a cost-based leaky bucket. `ShopifyClient` retries `THROTTLED` responses,
waiting just long enough for the bucket to refill based on `extensions.cost.throttleStatus`.
