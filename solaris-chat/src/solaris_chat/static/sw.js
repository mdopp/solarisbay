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
  var data = payload.data || {};
  var options = {
    body: payload.body || "",
    icon: "/static/apple-touch-icon.png",
    badge: "/static/solaris-mark.svg",
    data: data,
    // A chat push collapses per-session so a chatty turn-stream replaces rather
    // than stacks; a timer keeps its own tag (#713/#715).
    tag: data.timer_id || (data.kind === "chat" && data.session_id) || undefined,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  // Deep-link: a `chat` push carries `data.url` (#/c/<session>), a timer/card
  // opens the app root (#715).
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (clients) {
        for (var i = 0; i < clients.length; i++) {
          var c = clients[i];
          if ("focus" in c) {
            if ("navigate" in c && url !== "/") c.navigate(url);
            return c.focus();
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(url);
      })
  );
});
