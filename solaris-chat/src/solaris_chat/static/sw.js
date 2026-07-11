// Solaris push service worker (#713) — Web Push ONLY, no caching.
//
// #648 decided against a caching/offline service worker (it caused stale-UI
// deploys). This SW deliberately does nothing but receive push events and open
// the app on click, so the PWA can deliver timer/reminder notifications from
// the phone while the tab is closed. No `fetch` handler → no request caching.

self.addEventListener("push", function (event) {
  var payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { title: "Solaris", body: event.data ? event.data.text() : "" };
  }
  var title = payload.title || "Solaris";
  var options = {
    body: payload.body || "",
    icon: "/static/apple-touch-icon.png",
    badge: "/static/solaris-mark.svg",
    data: payload.data || {},
    tag: (payload.data && payload.data.timer_id) || undefined,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (clients) {
        for (var i = 0; i < clients.length; i++) {
          if ("focus" in clients[i]) return clients[i].focus();
        }
        if (self.clients.openWindow) return self.clients.openWindow("/");
      })
  );
});
