// register is injected as a global by Shopify's web pixel sandbox runtime
register(({ analytics, browser, init }) => {
  const shop = init.data.shop.myshopifyDomain;
  const ENDPOINT = "https://checkoutguardalerts.com/pixel-events";

  function beacon(event_name, extra) {
    browser.sendBeacon(
      ENDPOINT,
      JSON.stringify({ event_name, shop, ts: new Date().toISOString(), ...extra })
    );
  }

  analytics.subscribe("checkout_started", (event) => {
    beacon("checkout_started", {
      checkout_token: event.data.checkout.token,
    });
  });

  analytics.subscribe("checkout_completed", (event) => {
    beacon("checkout_completed", {
      checkout_token: event.data.checkout.token,
      order_id: String(event.data.checkout.order?.id ?? ""),
      total_price: event.data.checkout.totalPrice?.amount ?? null,
    });
  });

  analytics.subscribe("page_viewed", () => {
    beacon("page_viewed", {});
  });
});
