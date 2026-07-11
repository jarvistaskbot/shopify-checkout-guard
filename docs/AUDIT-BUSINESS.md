# Business Audit — Feature Value & Competitive Analysis

---

## 1. Feature-by-Feature Value Assessment

### Checkout Funnel Collapse Detector
**Value:** HIGH  
**Verdict:** KEEP  

The highest-signal detector. A 50% conversion rate drop is unambiguous — something is broken. Revenue impact estimate (missed_orders × AOV) is quantified and shown in the alert. This is the feature that justifies installation for most merchants.

**Differentiator:** Competitors test checkout synthetically (one fake purchase every 30 min). We detect real customer failures in real-time. A synthetic test passes; 1,000 real customers failing silently is caught.

**Would merchants pay for this alone?** Yes. If you catch one broken checkout event per year that lasts 2 hours on a $100K/month store (GMV ~$5.55/hr), that's $11/event caught. $29/month pays for itself in under 3 broken checkouts per year.

---

### Order Silence Detector
**Value:** MEDIUM-HIGH  
**Verdict:** KEEP with calibration improvements  

Catches the "store went down and Shopify didn't tell you" scenario. Day-of-week baseline is a good design choice. However:
- High false-positive risk for low-volume stores (a quiet Tuesday afternoon at a seasonal store = alert)
- Stripe outages cause order silence without any checkout failure — this detector catches those
- 3-check streak before alert (90 min of silence) is a good noise filter

**Challenge:** Order silence overlaps with abandonment spike and funnel collapse. Three alerts for one event creates noise. Alert deduplication across incident types would help.

**Would merchants pay for this alone?** Maybe. "No orders for 2 hours" is more ambiguous than "checkout broken." Some merchants would dismiss it as traffic-driven. The combination with funnel collapse detection is what makes it worth having.

---

### Abandonment Spike Detector
**Value:** MEDIUM  
**Verdict:** KEEP, but refine baseline logic  

A 3× abandonment rate AND >60% abandonment is a strong signal that something broke the checkout step between cart and purchase. However:
- Abandonment is inherently noisy (price shock, distraction, window shopping)
- The baseline calculation bug (window mismatch in `bl_orders`) means the baseline rate may be incorrect
- For new stores with <7 days of data, this never fires (baseline requires history)

**False positive risk:** Seasonal sales (like Black Friday price drops where everyone browses but few convert) could trigger abandonment spikes that aren't checkout failures. The 3× AND >60% threshold is aggressive enough to avoid most of these.

**Recommendation:** Add a merchant-configurable suppress window (e.g., "mute abandonment alerts during sale events").

---

### Payment Gateway Failure Detector
**Value:** HIGH  
**Verdict:** KEEP, fix auto-resolve  

Payment gateway issues are high-stakes — Stripe/PayPal outages are common enough that every merchant has experienced one. Three pending orders for 15+ minutes is a clear signal. The alert shows order names so the merchant can manually investigate.

**Critical gap:** Payment failure incidents never auto-resolve. This will cause accumulating "active incidents" in the dashboard even after the gateway recovers. Merchants will lose trust in the dashboard when they see incidents that are weeks old marked "Active."

**Fix required before v2 deploy:** Auto-resolve logic that checks if pending order count drops back to baseline.

---

### JS Error Spike Detector (v2)
**Value:** MEDIUM  
**Verdict:** KEEP, set expectations correctly  

The signal is real: a TypeError in cart.js that prevents "Add to Cart" from working will cause 100% abandonment — but silently, since Shopify never knows the button didn't work. Theme App Extension catches this.

**Limitation:** Most merchants can't act on a stack trace. "ReferenceError: $ is not defined at theme.liquid:423" means nothing to a Shopify merchant. The alert needs to say "Something may be broken on your product/cart pages — test them manually."

**Planned Haiku AI analysis** would significantly improve this: "Your jQuery dependency failed to load. This often happens after a theme update or app conflict. Check your theme's liquid files for recently added scripts."

**Would merchants pay for this alone?** No. JS error tracking for non-technical users is only valuable with actionable guidance. Alone it's noise; with AI analysis it becomes valuable.

---

### OOS Hot Product Detector (v2)
**Value:** HIGH  
**Verdict:** FIX THE BUG AND SHIP  

This is the most differentiated feature. No Shopify app currently alerts merchants the moment a hot product hits zero. Competitors (Back in Stock, Klaviyo) capture demand AFTER it's lost. We catch it the moment it happens.

**Revenue story is strong:** A product selling 10 units/week at $50 = $500/week. A 48-hour OOS window = $143 lost. One alert per month pays for the $29/month plan.

**Current state:** Completely non-functional due to product_id not being populated. Fix is well-understood (see 06-OOS-DETECTION.md). This feature alone could be the primary marketing hook.

**Would merchants pay for this alone?** Yes, if positioned correctly: "We alert you the moment your best-selling products hit zero inventory, before you lose sales."

---

## 2. Competitive Assessment

### vs. Uptime (31+ reviews, established)

Uptime uses synthetic checkout testing — it places a real test order every 30 minutes. This catches hard breaks (checkout 500 errors, payment gateway completely down). It does NOT detect:
- Partial failures (Add to Cart works for 80% of customers, fails for 20%)
- JS errors that silently prevent user actions
- Revenue impact quantification
- OOS hot products

**Our position:** "Uptime tells you if your checkout is broken. We tell you if it's quietly costing you money." — differentiated for conversion rate drops, JS errors, OOS.

**Their advantage:** Established brand, App Store reviews, years of data, more trusted. We need reviews to compete.

### vs. Revenue Shield / MyStoreGuardian

Similar synthetic testing approach. Same weaknesses as Uptime. No revenue attribution.

### vs. Raygun / CatchJS

Developer-facing JS error tracking. No Shopify-specific context, no revenue estimates, no merchant-readable alerts. Our Theme Extension + merchant-facing alerts are a clear win for non-technical merchants.

### vs. Shopify Analytics

Shopify's built-in analytics shows conversion funnel data but:
- No real-time alerting
- No anomaly detection
- No revenue impact estimates
- Delayed reporting (analytics lag)

We're complementary, not competitive, with Shopify's own tools.

---

## 3. Pricing Assessment

**Current:** $29/month, single plan  
**Spec:** $29/$79/$199/$399 four tiers

**Assessment:** The single $29 plan is the right starting point. Tier differentiation can be added post-launch when you have data on usage patterns. The risks of premature tiering:
- Higher tiers create friction for initial installs (merchants analyze which plan to pick)
- Without data on what "heavy users" want, tier features are guessed

**What to add to justify higher tiers eventually:**
- Growth ($79): Unlimited merchants (for agencies), weekly digest email, AI incident analysis
- Pro ($199): Multi-store dashboard, SLA (99.9% uptime), phone/priority support
- Agency ($399): White-label reports, custom thresholds, API access

**Trial design:** 14-day trial is exactly right per spec reasoning — first 7 days calibrating, second 7 days first real alerts. This is a strong trial conversion mechanism.

---

## 4. Missing Revenue-Protecting Features (Roadmap)

### Weekly Digest Email (Arto-approved, NOT built)
**Business value:** CRITICAL for retention  
Without periodic touchpoints, merchants forget the app is running. A Monday digest showing "last week: 0 incidents, conversion rate held at 68%, ~$0 estimated revenue protected" keeps the app top of mind even when things are going well.

Format:
```
Subject: CheckoutGuard — Your store was healthy last week ✅

Your checkout ran normally last week.
• Conversion rate: 68% (baseline 67% — stable)
• Checkouts: 847
• Orders: 575
• 0 incidents detected

Compare to stores like yours: average conversion rate on Shopify is 60-65%.
You're above average. Here's how to improve further: [3 tips]

— CheckoutGuard
```

This is a churn-defense feature, not a new detection feature. Must be built before reaching 10+ merchants.

### AI Incident Analysis via Claude Haiku (Arto-approved, NOT built)
**Business value:** HIGH — makes JS error alerts actionable for non-technical merchants

Integration:
1. When incident opens, call `claude-haiku-4-5-20251001` with incident detail
2. Prompt: "You are a Shopify expert. A merchant's store detected [incident_type]. Details: [detail JSON]. Write 2 sentences: what likely caused this, and what they should check first."
3. Store result in `incidents.detail.ai_analysis`
4. Include in Slack alert: "Likely cause: ..." 

Cost at scale: Haiku is ~$0.25/1M tokens. One incident analysis = ~500 tokens = $0.000125. At 1,000 incidents/month = $0.13/month. Negligible.

---

## 5. Business Risks

| Risk | Severity | Current status | Mitigation |
|---|---|---|---|
| App Store rejection | HIGH | v1 under review | Non-embedded UI is the main risk |
| False positives erode trust | HIGH | Noise floor + streak filters exist | Monitor first merchant's alert accuracy |
| Payment failure incidents stuck open | HIGH | Not resolved | Fix before v2 deploy |
| Merchants can't act on JS alerts | MEDIUM | No AI analysis yet | Build Haiku integration |
| OOS broken, not in v1 | MEDIUM | Known bug | Fix for v2 deploy |
| Zero reviews → cold start | MEDIUM | v1 in review | Offer white-glove onboarding to first 5 merchants |
| Revenue estimates wrong → distrust | MEDIUM | Estimates are conservative | Label "estimated" prominently |
| Shopify changes checkout API | LOW | Checkout webhooks stable | Monitor Shopify changelog |

---

## 6. Features to Reconsider

| Feature | Concern | Verdict |
|---|---|---|
| Abandonment spike | High false-positive risk for promotional periods | Keep, add suppress window |
| Order silence | Overlaps with funnel collapse; adds noise | Keep, but consolidate alerts if multiple fire |
| JS error spike raw alerts | Unactionable without AI analysis | Keep, but block on Haiku integration before marketing this feature |

---

## 7. Go-to-Market Observation

**Unique insight from competitor research (noted in spec):** "Merchants trust third-party monitoring apps more than Shopify's own status page. Shopify consistently understates outage severity."

This is CheckoutGuard's biggest marketing opportunity. During the next major Shopify outage:
- While Shopify's status page says "investigating"
- CheckoutGuard customers already received "CHECKOUT BROKEN" alerts with revenue estimates
- This builds word-of-mouth faster than any ad

**Target:** Get monitoring set up on 5 "influencer" Shopify stores (founders who are active on Twitter/X) before a major outage happens. One tweet ("CheckoutGuard caught the Shopify outage 20 minutes before Shopify's status page updated") is worth more than 100 App Store installs.
