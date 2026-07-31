/**
 * The client-side transport seam, mirroring `backend/src/restaurant/transport.py`.
 *
 * Components depend on this interface and on stores — never on mqtt.js. Swapping
 * or adding a transport means writing one implementation and adding one case to
 * the factory, with no component changes. Keeping the two sides symmetrical is
 * deliberate: the same words mean the same thing in both languages, so a reviewer
 * reading one already understands the other.
 */

import { loadConfig, type AppConfig } from './config';
import { connectMqtt } from './mqttClient';

export interface MessagingClient {
  /** This tab's identity, and the address its private error inbox is scoped to. */
  readonly clientId: string;

  /** Place an order. Resolves once the broker has acknowledged the publish. */
  publishOrder(tableId: number, foodName: string): Promise<void>;

  /** Ask the kitchen to clear all state. Demo affordance; see docs/SECURITY.md. */
  requestReset(): Promise<void>;

  disconnect(): Promise<void>;
}

/** Build the configured messaging client. */
export function createMessagingClient(
  config: AppConfig = loadConfig(),
): MessagingClient {
  switch (config.messagingBackend) {
    case 'mqtt':
      return connectMqtt(config);
    default: {
      // Exhaustiveness check: adding a backend to the union without handling it
      // here becomes a compile error rather than a runtime surprise.
      const unreachable: never = config.messagingBackend;
      throw new Error(`Unsupported messaging backend: ${String(unreachable)}`);
    }
  }
}
