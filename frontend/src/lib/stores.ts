/**
 * Svelte stores: the bridge between MQTT events and the rendered UI.
 *
 * A store is the right tool here rather than component state, because the data
 * arrives from *outside* the component tree — an MQTT callback, not a click. The
 * callback writes to a store and every subscribed component re-renders. In
 * markup, prefixing a store with `$` (e.g. `$tables`) reads its current value
 * and makes Svelte subscribe on mount and unsubscribe on destroy automatically,
 * which is what prevents the classic leak of a listener outliving its component.
 */

import { writable } from 'svelte/store';
import { applyTableState, emptyTableMap, type TableMap } from './state';
import type { KitchenStatus, OrderRejected, TableState } from './types';

export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline';

/** Authoritative table state, keyed by table id, as published by the kitchen. */
export const tables = writable<TableMap>(emptyTableMap);

/** Our own connection to the broker. */
export const connection = writable<ConnectionState>('connecting');

/**
 * Whether the kitchen process is up, per its retained status topic and Last Will.
 * `null` means we have not heard either way yet.
 */
export const kitchen = writable<KitchenStatus | null>(null);

/**
 * How far ahead the browser's clock runs relative to the kitchen's, in ms.
 *
 * Used to keep the cooking stopwatch honest when the two machines disagree.
 * Updated only from *live* messages — never retained ones, whose `updatedAt` can
 * be arbitrarily old and would poison the estimate with a fake offset.
 */
export const serverOffsetMs = writable(0);

export interface Notice {
  id: number;
  text: string;
  kind: 'error' | 'info';
}

export const notices = writable<Notice[]>([]);

const MAX_VISIBLE_NOTICES = 4;
const NOTICE_TIMEOUT_MS = 6000;
let noticeSequence = 0;

export function pushNotice(text: string, kind: Notice['kind'] = 'error'): number {
  const id = ++noticeSequence;
  notices.update((list) => [...list, { id, text, kind }].slice(-MAX_VISIBLE_NOTICES));
  // Auto-dismiss so a burst of rejections cannot permanently bury the UI.
  setTimeout(() => dismissNotice(id), NOTICE_TIMEOUT_MS);
  return id;
}

export function dismissNotice(id: number): void {
  notices.update((list) => list.filter((notice) => notice.id !== id));
}

export function ingestTableState(state: TableState, options?: { retained?: boolean }): void {
  tables.update((current) => applyTableState(current, state));
  if (options?.retained !== true) {
    // A live message was produced by the kitchen moments ago, so the gap between
    // its timestamp and ours approximates the clock difference (plus a little
    // network latency, which is negligible at second granularity).
    const producedAt = Date.parse(state.updatedAt);
    if (!Number.isNaN(producedAt)) {
      serverOffsetMs.set(Date.now() - producedAt);
    }
  }
}

export function ingestKitchenStatus(status: KitchenStatus): void {
  kitchen.set(status);
}

export function ingestRejection(rejection: OrderRejected): void {
  pushNotice(rejection.message, 'error');
}

/**
 * Reset for tests. Deliberately *not* called on disconnect: keeping the last
 * known state on screen behind a clear "reconnecting" banner is more useful than
 * blanking the tables, and the broker's retained messages re-sync us on
 * reconnect anyway.
 */
export function resetStores(): void {
  tables.set(emptyTableMap);
  connection.set('connecting');
  kitchen.set(null);
  notices.set([]);
}
