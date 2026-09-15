# CheckoutGuard — App Store Listing Draft (v1, 2026-09-15)

## App name (30 char limit)
CheckoutGuard

## Tagline / app card subtitle
Instant Slack alerts when your checkout breaks or revenue stops.

## App introduction (short)
CheckoutGuard watches your store's order flow around the clock and alerts
you in Slack the moment something is actively broken — a failing checkout,
a payment gateway outage, or a sudden dead stop in orders. Not analytics.
Not dashboards. An alarm for the moments that cost you money.

## Full description

**Your store can break silently.** A payment gateway hiccup, a broken
checkout after a theme update, an app conflict — and orders quietly stop
while your ads keep spending. Most merchants find out hours later, from
revenue they can never get back.

**CheckoutGuard finds out in minutes.** It learns your store's normal
order rhythm (day-of-week and hour-of-day aware) and fires a Slack alert
only when something is measurably wrong:

- **Checkout broken** — customers are starting checkouts but none are
  converting vs your baseline. "47 customers tried to buy, 0 orders
  completed."
- **Order silence** — zero orders during hours when your store always
  sells. "Expected ~8 orders/hr on Wednesday evening, none in 90 minutes."
- **Abandonment spike** — cart abandonment jumps to a multiple of your
  normal rate.
- **Payment failures** — orders piling up stuck in pending/failed states,
  pointing at your gateway.

When the incident ends, you get a recovery alert with the duration — so
you know it's over and what it cost.

**What CheckoutGuard is NOT:** it will not tell you sales are "slow," or
duplicate your analytics. If nobody wants to buy, that's marketing. If
people are trying to buy and failing — that's an incident, and that's
what we catch.

**Setup in 2 minutes:** install, paste your Slack webhook URL, done.
Baseline builds automatically over the first 7 days; detection turns on
by itself.

## Pricing
$29/month after a 14-day free trial. One plan, everything included.

## Key benefits (3 bullets for the card)
1. Know within minutes when checkout breaks — not hours later from a revenue report.
2. Day-of-week aware baseline: alerts on real anomalies, not normal quiet hours.
3. Slack-native: incidents and recovery notices where your team already looks.

## Screenshot plan (3-5 images, 1600x900)
1. Slack alert: "Revenue Drop Detected" message with baseline vs current + est. loss/min (the money shot).
2. Slack recovery alert with incident duration.
3. Onboarding page: paste Slack webhook → monitoring starts (shows 2-min setup).
4. Diagram slide: order flow -> baseline -> anomaly detection -> Slack (simple 4-step graphic).
5. Payment-failure alert example.

## Categories / tags
Category: Store management → Alerts and notifications (or "Operations").
Tags: monitoring, alerts, checkout, revenue, slack, uptime.

## Support & URLs
- Support email: support@checkoutguardalerts.com
- Website: https://checkoutguardalerts.com
- Privacy policy: https://checkoutguardalerts.com/privacy

## Review notes (for the "instructions to reviewer" field)
Test store instructions: install on a dev store, complete onboarding with
any Slack incoming-webhook URL. Detection requires a 7-day order baseline;
for review purposes the onboarding page confirms webhook delivery with a
test alert immediately. Scopes used: read_orders (order flow monitoring),
read_checkouts (abandonment/conversion detection). No customer PII is
stored beyond order timestamps and amounts; GDPR webhooks implemented.
