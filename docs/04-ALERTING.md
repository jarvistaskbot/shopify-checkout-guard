# Module: Alerting — Slack & Email Dispatch

**File:** `services/alerter.py`  
**Purpose:** Send formatted incident alerts to merchant Slack channels and email addresses.

---

## 1. Business Value

Alerting is the core value delivery mechanism — without it, the app is just a database. Merchants install CheckoutGuard specifically for the Slack message that says "your checkout is broken, here's the estimated revenue impact." Alert quality (signal-to-noise, formatting, actionability) directly drives retention.

---

## 2. Alert Functions

| Function | Incident type | Channels |
|---|---|---|
| `send_checkout_funnel_alert` | checkout_funnel_collapse | Slack + Email |
| `send_silence_alert` | volume_drop | Slack + Email |
| `send_abandonment_alert` | abandonment_spike | Slack + Email |
| `send_payment_failure_alert` | payment_failure | Slack + Email |
| `send_js_error_alert` | js_error_spike | Slack + Email |
| `send_oos_alert` | oos_hot_product | Slack + Email |
| `send_recovery_alert` | (any resolved) | Slack + Email |

---

## 3. Function Walk-Through

### `send_checkout_funnel_alert(webhook_url, shop_domain, incident_id, checkouts, orders, current_rate, baseline_rate, aov, alert_email)`

Constructs a Slack message with:
- Checkout vs order counts in last 30 min
- Conversion rate: current vs normal
- Estimated revenue at risk: `missed_orders × AOV`
- Incident ID (for tracking)
- Direct CTA: "Test your checkout NOW at https://{shop_domain}"

Email: strips Slack markdown (`*`, `:rotating_light:` → emoji), sends plain text.

**Revenue calculation:** `missed = max(0, int(checkouts * baseline_rate) - orders)`. This uses the baseline_rate applied to current checkouts — correct methodology, shows what should have completed.

### `send_silence_alert(webhook_url, shop_domain, incident_id, baseline, current_volume, aov, alert_email)`

Shows:
- Expected vs received order count per 30 min
- Drop percentage vs baseline
- Revenue at risk per hour: `baseline * 2 * aov` — reasoning: baseline is per 30-min slot, doubled = per-hour rate.

**Issue:** The silence alert says "No orders for 90+ min during expected peak hours" which is hardcoded in the message string. This is inaccurate — the detector fires after 3 consecutive 30-min drops (90 min of sustained silence). The message is correct in that sense, but doesn't reflect that it only fires during expected-high-volume periods (baseline ≥ 1.0 required).

### `send_abandonment_alert(webhook_url, shop_domain, incident_id, abandoned, checkouts, current_rate, baseline_rate, aov, alert_email)`

Shows:
- Abandoned vs total checkouts, percentage
- Multiplier vs normal rate
- Revenue at risk: `abandoned × AOV` — this overstates impact. Not all abandoned checkouts are recoverable and some abandonment is normal. However, the 3× multiplier condition means this only fires on truly abnormal events.

### `send_payment_failure_alert(webhook_url, shop_domain, incident_id, pending_count, order_names, total_at_risk, alert_email)`

Shows pending order names (up to 5) and total revenue stuck. Good signal-to-noise ratio.

**Issue:** `orders_str` uses `, ".join(order_names)` where `order_names` is already truncated to 5 in the detector. The logic at line 128 adds "and N more" if `pending_count > 5` but this is checking the original count against 5, while `order_names` is `orders[:5]` in the detector. Correct.

### `send_js_error_alert(webhook_url, alert_email, shop_domain, incident_id, count_10min, message, page_url)`

Shows error message (120 char truncated) and page URL. Correctly NO revenue estimate for JS errors — consistent with spec.

### `send_oos_alert(webhook_url, alert_email, shop_domain, incident_id, product_title, orders_last_7d, revenue_per_hour, unit_price)`

Shows product name, 7-day orders, estimated $/hr. Includes formula breakdown inline for transparency.

### `send_recovery_alert(webhook_url, alert_email, shop_domain, incident_id, duration_minutes, incident_type)`

Generic recovery alert for all incident types. Uses `_INCIDENT_LABELS` dict for human-readable type name.

---

## 4. Transport Functions

### `_post(webhook_url, text)` — Slack

- POST `{"text": text}` to the Slack Incoming Webhook URL.
- `resp.raise_for_status()` — propagates Slack errors to caller.
- Timeout: 10 seconds.
- If `webhook_url` is falsy: no-op (correct — some merchants may not have Slack configured).

**Issue:** Slack rate limits Incoming Webhooks to 1/second per webhook URL. If multiple incidents fire in rapid succession (funnel + silence + abandonment simultaneously), the last Slack call will fail with 429. No retry logic.

### `_send_email(to_email, subject, body)` — SendGrid

- Calls SendGrid v3 `/mail/send` API.
- From address: `alerts@checkoutguardalerts.com` (hardcoded).
- Logs warning on non-200/202 response.
- Silently swallows all exceptions (`except Exception: logger.error`).
- If `settings.sendgrid_api_key` is empty: no-op.
- Timeout: 15 seconds.

**Issue:** Plain text email only. No HTML version, no unsubscribe link. CAN-SPAM requires an unsubscribe mechanism for commercial emails. At low volume (1 merchant) this is low risk but must be addressed before scaling.

---

## 5. Error Handling

All send functions propagate exceptions to their callers in the detector. Detector callers wrap in try/except and log. This means failed alerts are logged but the incident is still created in DB.

**Issue:** When an alert fails, the `notified` column remains FALSE. There's no retry mechanism. If Slack/email is down when an alert fires, the merchant never receives the notification even though the incident is recorded.

---

## 6. Missing Functionality

| Missing | Impact |
|---|---|
| Weekly digest email | HIGH — merchants who don't receive incidents may forget the app is running; major churn risk |
| AI incident analysis (Haiku) | MEDIUM — "here's what may have caused this" adds intelligence layer |
| Alert deduplication / cooldown | MEDIUM — if same incident type fires repeatedly, merchant gets spammed |
| Retry on send failure | MEDIUM — transient Slack/SendGrid errors silently drop alerts |
| HTML email template | LOW — plain text works but looks unprofessional |
| Unsubscribe link in emails | LOW (legal risk for CAN-SPAM) |
| Multiple Slack channels per merchant | LOW — current design: one webhook URL per merchant |

---

## 7. Improvement Recommendations

1. Implement retry with exponential backoff for Slack + email sends (2-3 retries, 1s/2s/4s delays).
2. Add `notified_at` timestamp to incidents; build a background job that retries unnotified incidents.
3. Build the weekly digest — plain email summary: N incidents last week, current conversion rate, estimated savings. This is the #1 retention feature.
4. Integrate Haiku: after an incident is opened, run `claude-haiku-4-5` with incident detail + recent store events to generate a 1-sentence probable cause. Store in `incidents.detail.ai_cause`.
5. Add unsubscribe token to alert emails.
