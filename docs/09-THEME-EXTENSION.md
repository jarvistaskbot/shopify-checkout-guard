# Module: Theme App Extension — Error Tracker

**Files:**  
- `checkout-guard/extensions/error-tracker/shopify.extension.toml`  
- `checkout-guard/extensions/error-tracker/blocks/error-tracker.liquid`  
- `checkout-guard/extensions/error-tracker/assets/error-tracker.js`  
- `checkout-guard/shopify.app.toml`

**Purpose:** Client-side JavaScript injected into merchant storefronts to capture browser errors and send them to the `/events` endpoint.

---

## 1. What It Does

The Theme App Extension adds a Liquid block that merchants (or the app automatically) include in cart and product page templates. The JS:
1. Registers `window.error` and `window.unhandledrejection` event listeners.
2. Queues captured errors in a local array.
3. Flushes every 10 seconds (or on page unload via `sendBeacon`).
4. POSTs batched errors to `https://checkoutguardalerts.com/events`.

---

## 2. Files

### `shopify.extension.toml`

```toml
api_version = "2024-10"
[[extensions]]
name = "CheckoutGuard Error Tracker"
handle = "error-tracker"
type = "theme_app_extension"
```

Minimal, correct.

### `blocks/error-tracker.liquid`

The Liquid template that renders into the merchant's theme. Contains two independent implementations:

**1. Inline JavaScript (in `<script>` tag):**
- Self-contained IIFE
- Gets shop domain from `{{ shop.permanent_domain | json }}`
- Hardcodes `ENDPOINT = "https://checkoutguardalerts.com/events"`
- Queue: `queue.splice(0)` — empties entire queue on flush
- MAX_QUEUE: implicit (unbounded in inline version)
- Sends via `navigator.sendBeacon` with fallback to `fetch`

**2. Schema reference to external asset:**
```json
{% schema %}
{
  "name": "CheckoutGuard Error Tracker",
  "target": "section",
  "javascript": "error-tracker.js"
}
{% endschema %}
```

Shopify loads `assets/error-tracker.js` when this block is rendered.

**CRITICAL ISSUE: DOUBLE EXECUTION**

Both the inline `<script>` block AND the `error-tracker.js` asset will load and execute. Both register `window.addEventListener("error", ...)` and `window.addEventListener("unhandledrejection", ...)`. Every browser error will be captured twice and sent in two separate batches to the `/events` endpoint.

The `/events` endpoint will receive duplicates. The `error_hash` deduplication in the detector (24h baseline check) won't deduplicate within the same batch — it uses a hash of (message|source), so two identical events in the same flush will both be inserted. The 10-min count will be 2× actual error frequency, potentially triggering false spike incidents at 5 errors (appearing as 10).

**Fix:** Remove the inline `<script>` block from the Liquid template and rely solely on the `"javascript"` schema reference. OR remove the `"javascript"` schema reference and keep the inline script. Do not use both.

### `assets/error-tracker.js`

The external asset. Slightly cleaner than the inline version:
- Uses `document.currentScript || document.querySelector("[data-cg-shop]")` to get shop domain from a `data-cg-shop` attribute — but the Liquid block doesn't add `data-cg-shop` to any element. The script falls back to `document.querySelector("[data-cg-shop]")` which also won't match. This path `if (!shop) return` exits silently.

**Wait — how does the JS asset get the shop domain?** The schema reference `"javascript": "error-tracker.js"` loads the asset as an external script. But the Liquid block's inline variables (like `var SHOP = {{ shop.permanent_domain | json }}`) are NOT available in the external asset — that's a different script execution context. The asset JS tries to get the shop via `document.currentScript.getAttribute("data-cg-shop")` which will be null for an externally-loaded script. **The asset JS never successfully gets a shop domain and silently exits every time.**

This means:
- **Inline script**: Works correctly (has shop domain via Liquid interpolation). Double-registers listeners.
- **Asset script**: Always exits at `if (!shop) return`. Does nothing.

Net effect: The Liquid inline script is the only functional code path. The schema `"javascript"` reference loads a script that does nothing. This is confusing but not actively harmful (no double-capture). 

**Recommendation:** Remove the `"javascript": "error-tracker.js"` reference from the schema, OR rewrite the asset to receive shop domain via a `data-` attribute set by Liquid in the `<script>` tag.

---

## 3. JS Logic (Functional Path: Inline Script)

```javascript
(function() {
  var SHOP = {{ shop.permanent_domain | json }};  // Liquid injection
  var ENDPOINT = "https://checkoutguardalerts.com/events";
  var BATCH_INTERVAL = 10000;  // 10 seconds
  var queue = [];
  var timer = null;

  function flush() {
    if (!queue.length) return;
    var events = queue.splice(0);  // drain entire queue
    // sendBeacon or fetch with keepalive
  }

  function enqueue(message, source) {
    queue.push({ shop, message, source, url, ts });
    if (!timer) timer = setTimeout(() => { timer = null; flush(); }, BATCH_INTERVAL);
  }

  window.addEventListener("error", e => enqueue(e.message, e.filename));
  window.addEventListener("unhandledrejection", e => enqueue(String(e.reason...), "promise"));
})();
```

**Timer behavior:** First error starts a 10s timer. All errors in the next 10s accumulate in the queue. At 10s, flush fires and timer is cleared. Next error starts a new 10s window. This is correct batching behavior.

**On page unload:** `navigator.sendBeacon` is used instead of `fetch` — correct. `sendBeacon` sends the request asynchronously even as the page unloads. The `fetch` with `keepalive: true` fallback is also correct.

**Max queue:** Inline script has `queue.splice(0)` which drains the entire queue without limit. If many errors fire rapidly, the queue grows unboundedly. The asset version has `MAX_QUEUE = 20` as a cap. Inline version is missing this cap.

---

## 4. Scope: Cart and Product Pages Only

The Liquid block comment correctly notes:
```liquid
{% comment %}
  Add to product and cart templates in the Theme Editor.
  DO NOT add to checkout templates (checkout.liquid deprecated for non-Plus stores).
{% endcomment %}
```

This aligns with the spec. The extension cannot run on checkout pages for non-Shopify Plus stores (Shopify doesn't allow third-party scripts on checkout pages). The block targets pre-checkout pages where JS errors that prevent "Add to Cart" can be caught.

---

## 5. Payload Format

Each event sent:
```json
{
  "shop": "store.myshopify.com",
  "message": "TypeError: Cannot read property...",
  "source": "theme.js" | "promise",
  "url": "https://store.myshopify.com/products/widget",
  "ts": 1720000000000
}
```

The server `/events` endpoint also accepts `lineno` and `colno` but these are not sent by either version of the JS.

---

## 6. Improvement Recommendations

1. **Remove the `"javascript": "error-tracker.js"` schema reference** to eliminate confusion, since the asset JS can't get the shop domain without Liquid.
2. **Add MAX_QUEUE cap to inline script** (match asset version's 20-item limit).
3. **Add `lineno` and `colno`** to the payload for richer debugging.
4. **Consider adding `data-cg-shop` attribute** to a DOM element set by Liquid so the asset JS could work independently of Liquid context in future.
5. **Add error filtering**: browser extensions inject noise. Filter errors with empty filenames or `chrome-extension://` sources before queuing.
