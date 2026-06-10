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
  let payload = { title: 'LightChat', body: 'New message' };
  try {
    payload = event.data.json();
  } catch(e) {
    if (event.data) payload.body = event.data.text();
  }

  let options;
  if (payload.data && payload.data.type === 'incoming_call') {
    options = {
      body: payload.body,
      icon: '/lightchat-icon.png',
      badge: '/lightchat-icon.png',
      tag: 'lightchat-call',
      renotify: true,
      requireInteraction: true,
      vibrate: [400, 150, 400, 150, 600],
      actions: [
        { action: 'answer', title: 'Answer' },
        { action: 'decline', title: 'Decline' }
      ],
      data: payload.data
    };
  } else {
    options = {
      body: payload.body,
      icon: '/lightchat-icon.png',
      badge: '/lightchat-icon.png',
      tag: 'lightchat-message',
      renotify: true,
      vibrate: [200, 100, 200],
      data: { url: (payload.data && payload.data.url) || 'https://lightchat.chat/' }
    };
  }

  event.waitUntil(
    self.registration.showNotification(payload.title || 'LightChat', options)
  );
});

// ── Notification click: focus the app or open it ──
self.addEventListener('notificationclick', function(event) {
  event.notification.close();

  if (event.notification.data && event.notification.data.type === 'incoming_call') {
    const callData = event.notification.data;
    event.waitUntil(
      clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
        // Post message to all app clients so they can show incoming call UI
        for (let client of list) {
          client.postMessage({
            type: 'incoming_call_tap',
            caller_wallet: callData.caller_wallet,
            caller_handle: callData.caller_handle
          });
        }
        // Focus an existing window or open a new one
        for (let client of list) {
          if (client.url.includes('lightchat.chat') && 'focus' in client) {
            return client.focus();
          }
        }
        if (clients.openWindow) {
          return clients.openWindow(callData.url || 'https://lightchat.chat/');
        }
      })
    );
    return;
  }

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
