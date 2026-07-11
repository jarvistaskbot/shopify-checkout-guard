/**
 * CheckoutGuard Error Tracker — vanilla JS, <5KB
 * Loaded via Theme App Extension block on cart and product pages.
 * The shop domain is injected by the Liquid block via data attribute on the script tag.
 */
(function () {
  var container = document.currentScript || document.querySelector("[data-cg-shop]");
  var shop = container ? container.getAttribute("data-cg-shop") : null;
  if (!shop) return;

  var ENDPOINT = "https://checkoutguardalerts.com/events";
  var BATCH_INTERVAL = 10000;
  var MAX_QUEUE = 20;
  var queue = [];
  var timer = null;

  function flush() {
    if (!queue.length) return;
    var events = queue.splice(0, MAX_QUEUE);
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(
          ENDPOINT,
          new Blob([JSON.stringify(events)], { type: "application/json" })
        );
      } else {
        fetch(ENDPOINT, {
          method: "POST",
          body: JSON.stringify(events),
          headers: { "Content-Type": "application/json" },
          keepalive: true,
        });
      }
    } catch (_) {}
  }

  function enqueue(message, source) {
    if (queue.length >= MAX_QUEUE) return;
    queue.push({
      shop: shop,
      message: String(message || "").slice(0, 500),
      source: String(source || "").slice(0, 200),
      url: window.location.href,
      ts: Date.now(),
    });
    if (!timer) {
      timer = setTimeout(function () {
        timer = null;
        flush();
      }, BATCH_INTERVAL);
    }
  }

  window.addEventListener("error", function (evt) {
    enqueue(evt.message || "unknown error", evt.filename);
  });

  window.addEventListener("unhandledrejection", function (evt) {
    var reason = evt.reason;
    var msg = reason && reason.message ? reason.message : String(reason || "unhandled rejection");
    enqueue(msg, "promise");
  });
})();
