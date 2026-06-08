// LightChat Service Worker — push notifications

self.addEventListener('push', function(event) {
  let data = { title: 'LightChat', body: 'New message' };
  try {
    data = event.data.json();
  } catch(e) {
    if (event.data) data.body = event.data.text();
  }

  const options = {
    body: data.body,
    icon: 'https://keiko-dev-lcai.github.io/lightchat-app/icon-192.png',
    badge: 'https://keiko-dev-lcai.github.io/lightchat-app/icon-192.png',
    tag: 'lightchat-message',
    renotify: true,
    vibrate: [200, 100, 200],
    data: { url: self.location.origin + '/lightchat-app/' }
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'LightChat', options)
  );
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url)
    ? event.notification.data.url
    : 'https://keiko-dev-lcai.github.io/lightchat-app/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
      for (let client of list) {
        if (client.url.includes('lightchat-app') && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});
