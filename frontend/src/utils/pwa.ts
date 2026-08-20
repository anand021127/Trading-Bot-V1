/**
 * PWA Service Worker Registration & Lifecycle Management
 */

let registrationInstance: ServiceWorkerRegistration | null = null;

export function registerServiceWorker(onUpdateAvailable?: (reg: ServiceWorkerRegistration) => void) {
  if (typeof window === 'undefined' || !('serviceWorker' in navigator)) {
    return;
  }

  window.addEventListener('load', async () => {
    try {
      const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      registrationInstance = reg;

      // Check if there is already a worker waiting
      if (reg.waiting) {
        onUpdateAvailable?.(reg);
      }

      // Detect when a new service worker is installing
      reg.addEventListener('updatefound', () => {
        const newWorker = reg.installing;
        if (!newWorker) return;

        newWorker.addEventListener('statechange', () => {
          if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
            // New version available
            console.log('[PWA] New application version detected.');
            onUpdateAvailable?.(reg);
          }
        });
      });

      // Periodically check for updates every 15 minutes
      setInterval(() => {
        reg.update().catch(() => {
          // ignore background update errors
        });
      }, 15 * 60 * 1000);

      // Handle controller change (when new worker takes over)
      let refreshing = false;
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (!refreshing) {
          refreshing = true;
          window.location.reload();
        }
      });
    } catch (err) {
      console.warn('[PWA] Service worker registration failed:', err);
    }
  });
}

export function triggerPWAUpdate() {
  if (registrationInstance?.waiting) {
    registrationInstance.waiting.postMessage({ type: 'SKIP_WAITING' });
  } else {
    window.location.reload();
  }
}
