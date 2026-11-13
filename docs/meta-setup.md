# Connecting WhatsApp, Messenger and Instagram

All three channels are configured in one Meta app at <https://developers.facebook.com>.
ShopAssist exposes a single webhook URL for all of them: `https://<your-host>/webhooks/meta`.

## 1. Create the app and webhook

1. Create a **Business** app and add the **WhatsApp**, **Messenger** and **Instagram** products.
2. In *App settings → Basic*, copy the **App Secret** into `META_APP_SECRET`.
3. Pick any long random string for `META_VERIFY_TOKEN`.
4. Configure the webhook callback URL `https://<host>/webhooks/meta` with that verify token for:
   - **WhatsApp Business Account**, field `messages`
   - **Page**, fields `messages`, `messaging_postbacks` (Part 3 also uses `messaging_handovers`, `standby`)
   - **Instagram**, fields `messages`, `messaging_postbacks`, `comments` (Part 3 also uses `messaging_handovers`, `standby`)

For local development, expose port 8000 with a tunnel (e.g. `cloudflared tunnel --url http://localhost:8000`).

## 2. WhatsApp Cloud API

- `WHATSAPP_PHONE_NUMBER_ID`: from *WhatsApp → API Setup*.
- `WHATSAPP_TOKEN`: create a **System User** in Business Settings, give it the app with
  `whatsapp_business_messaging` and `whatsapp_business_management`, and generate a permanent token.
- Free-form replies are only allowed inside the **24-hour customer-service window** (24 hours
  from the customer's last message). Outside it you need an approved template.
- Meta's WhatsApp Business Solution terms (in force since 15 January 2026) bar *general-purpose*
  AI assistants. Business-specific support bots like this one are allowed, which is why the prompt
  hard-scopes the agent to the store's topics.

## 3. Messenger

- `PAGE_ID` and `PAGE_ACCESS_TOKEN`: a Page token with `pages_messaging`.
- Replies use `messaging_type: RESPONSE` (inside the 24-hour window).

## 4. Instagram (DMs + comments)

ShopAssist defaults to the **Instagram API with Facebook Login** (IG professional account
linked to the Page, Page token, `graph.facebook.com`). Permissions: `instagram_basic`,
`instagram_manage_messages`, `instagram_manage_comments`, `pages_messaging`.

If you use **Instagram API with Instagram Login** instead, set:

```
INSTAGRAM_API_HOST=https://graph.instagram.com
INSTAGRAM_ACCESS_TOKEN=<IG user token>
INSTAGRAM_MESSAGING_NODE=<IG professional account id>
```

and add the Instagram app secret to the signature check (see `app/channels/webhook.py`).

**Comments**: public replies go to `POST /{comment-id}/replies`. Anything that needs personal
details moves to DM via a **private reply** (`recipient.comment_id`). Meta allows one private reply
per comment, sent within 7 days of the comment.

## 5. API versions

`META_GRAPH_VERSION` is set in one place (default `v27.0`). Meta keeps each version usable for
about two years after the next one ships, so plan to bump it about once a year.
