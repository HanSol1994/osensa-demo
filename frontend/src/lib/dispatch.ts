/**
 * Inbound message routing.
 *
 * Separated from the MQTT client so it can be tested by calling a function with
 * a topic and a string, with no broker, no WebSocket and no fake timers. This is
 * where every untrusted payload is validated before it reaches a store.
 */

import { ingestKitchenStatus, ingestRejection, ingestTableState } from './stores';
import { KITCHEN_STATUS, isClientErrorTopic, isTableStateTopic } from './topics';
import {
  parseKitchenStatus,
  parseOrderRejected,
  parseTableState,
  safeJsonParse,
} from './types';

/** Outcome of routing one message. Returned so tests can assert on it. */
export type DispatchResult =
  | 'table-state'
  | 'kitchen-status'
  | 'rejection'
  | 'ignored-unknown-topic'
  | 'ignored-malformed';

export interface DispatchOptions {
  /**
   * Whether the broker delivered this as a retained message. Matters because a
   * retained payload can be arbitrarily old, so it must not be used to estimate
   * the server/browser clock offset.
   */
  retained?: boolean;
}

export function dispatchMessage(
  topic: string,
  payload: string,
  clientId: string,
  options: DispatchOptions = {},
): DispatchResult {
  const raw = safeJsonParse(payload);
  if (raw === undefined) {
    console.warn('[mqtt] dropped non-JSON payload', { topic });
    return 'ignored-malformed';
  }

  if (isTableStateTopic(topic)) {
    const state = parseTableState(raw);
    if (state === null) {
      console.warn('[mqtt] dropped malformed table state', { topic });
      return 'ignored-malformed';
    }
    ingestTableState(state, { retained: options.retained });
    return 'table-state';
  }

  if (topic === KITCHEN_STATUS) {
    const status = parseKitchenStatus(raw);
    if (status === null) {
      console.warn('[mqtt] dropped malformed kitchen status', { topic });
      return 'ignored-malformed';
    }
    ingestKitchenStatus(status);
    return 'kitchen-status';
  }

  if (isClientErrorTopic(topic, clientId)) {
    const rejection = parseOrderRejected(raw);
    if (rejection === null) {
      console.warn('[mqtt] dropped malformed rejection', { topic });
      return 'ignored-malformed';
    }
    ingestRejection(rejection);
    return 'rejection';
  }

  // We are subscribed to nothing else, so this means a broker misconfiguration
  // or someone else's traffic. Log rather than guess.
  console.warn('[mqtt] message on unexpected topic', { topic });
  return 'ignored-unknown-topic';
}
