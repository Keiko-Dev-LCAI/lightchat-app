// LightChat Service Worker — push notifications + PWA lifecycle

// ── Lifecycle: install & activate immediately so the SW takes control fast ──
self.addEventListener('install', function(event) {
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  // Claim all open clients so the new SW is active right away
  event.waitUntil(clients.claim());
});

// ── Fetch handler (required for iOS to treat this as a fully active SW) ──
// We don't do offline caching — just pass every request straight through.
self.addEventListener('fetch', function(event) {
  event.respondWith(fetch(event.request));
});

// ── Push: show a notification when the server sends a push message ──
self.addEventListener('push', function(event) {
  let data = { title: 'LightChat', body: 'New message' };
  try {
    data = event.data.json();
  } catch(e) {
    if (event.data) data.body = event.data.text();
  }

  const options = {
    body: data.body,
    icon: '/lightchat-icon.png',
    badge: '/lightchat-icon.png',
    tag: 'lightchat-message',
    renotify: true,
    vibrate: [200, 100, 200],
    data: { url: 'https://lightchat.chat/' }
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'LightChat', options)
  );
});

// ── Notification click: focus the app or open it ──
self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url)
    ? event.notification.data.url
    : 'https://lightchat.chat/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
      for (let client of list) {
        if (client.url.includes('lightchat.chat') && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});
