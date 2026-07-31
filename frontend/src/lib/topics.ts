/** Topic construction and matching. Mirrors backend/src/restaurant/topics.py. */

const PREFIX = 'restaurant';

export const TABLE_STATE_SUBSCRIPTION = `${PREFIX}/table/+/state`;
export const KITCHEN_STATUS = `${PREFIX}/kitchen/status`;

/** Outbound command: clear all state. A demo affordance — see docs/SECURITY.md. */
export const KITCHEN_RESET = `${PREFIX}/kitchen/reset`;

export function orderTopic(tableId: number): string {
  return `${PREFIX}/table/${tableId}/order`;
}

export function clientErrorTopic(clientId: string): string {
  return `${PREFIX}/client/${clientId}/err`;
}

/**
 * True if the topic is a table state topic. Used to route inbound messages.
 *
 * Matched explicitly rather than by a loose `includes()` so that an unexpected
 * topic is routed nowhere instead of into the wrong handler.
 */
export function isTableStateTopic(topic: string): boolean {
  const parts = topic.split('/');
  return (
    parts.length === 4 &&
    parts[0] === PREFIX &&
    parts[1] === 'table' &&
    parts[3] === 'state'
  );
}

export function isClientErrorTopic(topic: string, clientId: string): boolean {
  return topic === clientErrorTopic(clientId);
}
