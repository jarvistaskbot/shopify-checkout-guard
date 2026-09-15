# CheckoutGuard — Launch Playbook (step by step)

Written 2026-09-15, night of App Store approval. App is APPROVED + PUBLISHED (unlisted).
Direct install link (works while unlisted): the listing URL from Dev Dashboard → Distribution.

Legend: [YOU] = Arto in a dashboard/browser. [ME] = assistant does it on request.

---

## STEP 0 — Money plumbing (tomorrow, 15 min, one-time)

0.1 [YOU] Taxes: NOTHING REQUIRED. (Correction 2026-09-15: Shopify Partners
    have no W-8BEN interview — that's an Apple/Google pattern. The "Taxes"
    block in Partner Settings only has OPTIONAL dropdowns for US tax
    registration and EU VAT registration; Arto has neither → leave both
    empty. If Shopify ever needs tax info, it prompts in the dashboard.)

0.2 [YOU] Partner Dashboard → look for "Revenue share" / plan registration
    (may be under the app's Distribution or Partner settings).
    - Register the app for the reduced revenue-share plan: 0% on first $1M/yr
      (15% above). Without registering you pay the legacy 20% from dollar one.

0.3 [YOU] Payouts page → confirm wire-transfer method shows as verified/active,
    and set payout threshold/schedule so wires batch (>= $250 recommended;
    each SWIFT wire costs fees on both ends).

DONE WHEN: tax form accepted, rev-share registered, payout method active.

---

## STEP 1 — Prepare outreach assets (I do this, same day)

1.1 [ME] Final r/shopify post (draft below, Arto approves wording).
1.2 [ME] Short DM/email pitch for direct outreach (draft below).
1.3 [ME] Fire 2 clean test alerts (drop + recovery) to a tidy Slack channel
    so Arto can screenshot them for the post/listing refresh.
1.4 [YOU] Screenshot the 2 alerts on desktop Slack, light theme, ~1600 px wide.

### r/shopify post draft (validation-style, not an ad — follow sub rules,
### flair "App" or "Tool" if required, no naked self-promo)

Title: I built a tool that pings you in Slack the moment your checkout
breaks — looking for 5 stores to try it free

Body:
> Merchant problem I kept seeing: checkout silently breaks (payment gateway
> hiccup, theme update, app conflict) and you find out hours later from a
> revenue dip — while ads keep spending.
>
> I built CheckoutGuard: it learns your store's normal order rhythm
> (hour-of-day / day-of-week aware) and sends a Slack alert within minutes
> when something is measurably wrong — checkout conversion collapse, order
> silence during your peak hours, abandonment spike, payments stuck pending.
> It does NOT duplicate analytics and it won't nag you that "sales are slow."
> If nobody wants to buy, that's marketing. If people are trying to buy and
> failing — that's an incident. That's the only thing it alerts on.
>
> It just passed Shopify review. I'm looking for the first ~5 stores to run
> it: extended free trial, direct line to me (the developer), and I'll build
> what you actually need. Setup is 2 minutes (install, paste Slack webhook).
> Comment or DM if you want the link.

### Direct DM / email pitch (for store owners you find in communities)

> Hi <name> — saw your post about <context>. I'm the developer of
> CheckoutGuard, a Shopify app that Slack-alerts you within minutes if your
> checkout breaks or orders flatline during hours your store normally sells
> (it just passed Shopify's app review). I'm onboarding the first 5 stores
> with an extended free trial in exchange for honest feedback. 2-minute
> setup, read-only access to orders/checkouts, nothing touches your theme.
> Want the install link?

---

## STEP 2 — First merchants via direct link (this week, unlisted)

2.1 [YOU] Post the r/shopify thread (best 15:00-19:00 UTC weekday).
2.2 [YOU] Send the DM pitch to 10-15 store owners (r/shopify, r/ecommerce,
    Shopify Community forum threads about downtime/lost sales, X).
2.3 [ME] Watcher already running on Mac mini — Telegram pings on any real
    install. On each install I check onboarding completion + webhook health
    from VPS logs and flag stores that stall so you can follow up.
2.4 [YOU/ME] For each installed store: after ~1 week of clean running, ask
    for an honest App Store review (in-app or via email). Target: 2-3 reviews.

GATE: >= 3 active stores AND >= 2 reviews → go to Step 3.
(If 2 weeks pass with zero traction, we stop and reassess positioning
before spending anything on ads.)

---

## STEP 3 — Go public ("open the shop")

3.1 [YOU] Dev Dashboard → CheckoutGuard → Distribution / listing →
    "Make fully visible". No re-review; effective immediately.
3.2 [ME] Same day: refresh listing copy if needed (LISTING.md text, corrected
    4-tier pricing), confirm screenshots/icon look right on the public page.
3.3 [ME] Verify the public listing renders (search for "CheckoutGuard" in the
    App Store, check categories/tags).

---

## STEP 4 — Ads (only after Step 3 + reviews exist)

4.1 [YOU] Partner Dashboard → Apps → App ads → Create ad → Search ad.
4.2 Settings for campaign #1 (conservative probe):
    - Keywords (exact-intent only): checkout monitoring, downtime alert,
      revenue alert, store uptime, order monitoring, slack alerts
    - AVOID broad: analytics, reports, sales (giant competitors, bad CPC).
    - Bid: start LOW ($0.30-0.50/click) — first-price auction: you pay
      exactly your bid. Raise only if zero impressions after 2-3 days.
    - Daily budget: $5-10. Geotarget: US, CA, UK, AU first.
4.3 [ME] Weekly readout from installs vs ad spend: CPC → install rate →
    trial→paid conversion. Scale budget only when CAC < 1 month of revenue
    of the median converting plan.
4.4 Billing for ads: Settings → Ad billing (separate card).

---

## STEP 5 — Post-launch product items (parallel, low urgency)

5.1 [ME] Request read_inventory/read_products scopes properly → enable OOS
    feature → restore Pro-plan OOS listing line.
5.2 [ME] Rotate review-workspace Slack secrets (were pasted in chat 2026-07-22).
5.3 [ME] Fix pre-existing weekday-mismatch bug in _compute_silence_baseline
    (Python weekday() vs Postgres DOW) — needs Arto's go, changes live
    detector behavior.
5.4 Kill the review watcher when its first-merchant duty is done:
    pkill -f checkoutguard_review_watch

---

## Metrics to watch weekly (I can automate a Telegram digest on request)
- Installs (watcher), onboarding completion rate, trial→paid conversions,
  MRR, alert deliveries (real incidents caught = the retention driver),
  uninstalls + reason if any.
