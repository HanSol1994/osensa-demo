/**
 * Runtime configuration, read from Vite env vars at build time.
 *
 * Note honestly: anything here ends up in the public JavaScript bundle. The
 * broker credentials are therefore not secrets, and security depends on the
 * broker ACL restricting this user to publishing orders only. See the trust
 * model section of docs/EVENT-CONTRACT.md.
 */

/** Which messaging implementation to build. Mirrors the backend's setting. */
export const MESSAGING_BACKENDS = ['mqtt'] as const;
export type MessagingBackend = (typeof MESSAGING_BACKENDS)[number];

export interface AppConfig {
  messagingBackend: MessagingBackend;
  brokerUrl: string;
  username: string | undefined;
  password: string | undefined;
}

const DEFAULT_BROKER_URL = 'ws://localhost:9001/mqtt';

export function loadConfig(env: ImportMetaEnv = import.meta.env): AppConfig {
  const url = env.VITE_MQTT_URL?.trim();
  const backend = env.VITE_MESSAGING_BACKEND?.trim();
  return {
    // Falls back rather than throwing on an unknown value: a typo in an env var
    // should not produce a blank page.
    messagingBackend: isMessagingBackend(backend) ? backend : 'mqtt',
    brokerUrl: url && url.length > 0 ? url : DEFAULT_BROKER_URL,
    username: env.VITE_MQTT_USERNAME || undefined,
    password: env.VITE_MQTT_PASSWORD || undefined,
  };
}

function isMessagingBackend(value: string | undefined): value is MessagingBackend {
  return (
    value !== undefined && (MESSAGING_BACKENDS as readonly string[]).includes(value)
  );
}

const CLIENT_ID_STORAGE_KEY = 'restaurant.clientId';

/**
 * A stable identity for this browser tab.
 *
 * Stored in `sessionStorage`, which is scoped per tab: two windows get two
 * identities (so each has its own private error inbox and neither steals the
 * other's broker session), while a refresh of one window keeps its identity.
 * Using `localStorage` would break this — both windows would share an id and the
 * broker would disconnect one as a duplicate client.
 */
export function getClientId(): string {
  const existing = sessionStorage.getItem(CLIENT_ID_STORAGE_KEY);
  if (existing !== null && existing.length > 0) {
    return existing;
  }
  const fresh = generateId();
  sessionStorage.setItem(CLIENT_ID_STORAGE_KEY, fresh);
  return fresh;
}

/** `crypto.randomUUID` needs a secure context, which plain-http LAN access is not. */
export function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().replace(/-/g, '');
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}
