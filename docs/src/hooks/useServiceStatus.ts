import {useCallback, useSyncExternalStore} from 'react';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';

export type ServiceStatus = 'checking' | 'up' | 'down' | 'unknown';

const TIMEOUT_MS = 5000;
const INTERVAL_MS = 30_000;

// The health route sits at the root of the host that serves the playground, so the address
// comes from the same build setting. An empty setting leaves the status unknown.
function healthUrl(serviceUrl: string): string | null {
  try {
    return new URL('/healthz', serviceUrl).href;
  } catch {
    return null;
  }
}

async function probe(url: string): Promise<boolean> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    return (await fetch(url, {cache: 'no-store', signal: controller.signal})).ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

// One store for the whole site, so the footer and the playground share a single probe. It
// polls while at least one component is subscribed.
let current: ServiceStatus = 'checking';
let timer: ReturnType<typeof setInterval> | undefined;
const listeners = new Set<() => void>();

function publish(next: ServiceStatus): void {
  if (next === current) return;
  current = next;
  listeners.forEach((notify) => notify());
}

function start(url: string): void {
  const check = () => probe(url).then((ok) => publish(ok ? 'up' : 'down'));
  void check();
  timer = setInterval(check, INTERVAL_MS);
}

/** Reports whether the hosted service passes its health check, re-checked every 30 seconds. */
export default function useServiceStatus(): ServiceStatus {
  const {siteConfig} = useDocusaurusContext();
  const url = healthUrl(String(siteConfig.customFields?.playgroundUrl ?? ''));

  // Stable between renders, or React would unsubscribe and subscribe again on each one.
  const subscribe = useCallback(
    (notify: () => void) => {
      if (!url) return () => undefined;
      listeners.add(notify);
      if (listeners.size === 1) start(url);
      return () => {
        listeners.delete(notify);
        if (listeners.size === 0) clearInterval(timer);
      };
    },
    [url],
  );
  const status = useSyncExternalStore(subscribe, () => current, (): ServiceStatus => 'checking');

  return url ? status : 'unknown';
}
