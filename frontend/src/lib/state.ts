/**
 * Pure state transitions.
 *
 * Kept free of Svelte and of MQTT so the interesting logic — the version guard,
 * which is the trickiest correctness rule in the client — can be unit tested as
 * plain functions with no DOM, no broker and no component harness.
 */

import type { Order, TableState } from './types';

export type TableMap = ReadonlyMap<number, TableState>;

export const emptyTableMap: TableMap = new Map();

/** Control characters: C0 range plus DEL. Same rule as the backend validator. */
const CONTROL_CHARACTERS = /[\u0000-\u001F\u007F]/;

/**
 * Apply an inbound table snapshot, ignoring stale ones.
 *
 * On reconnect a client can receive the broker's retained message *and* a live
 * update with no ordering guarantee between them, so the rule is: only accept a
 * snapshot whose version is strictly greater than the one already held.
 *
 * Versions are only comparable *within one kitchen generation*. Kitchen state is
 * in-memory, so a restart resets versions to zero — and a plain version check
 * would then reject the restarted kitchen's state forever and leave dead orders
 * on screen. A changed `epoch` means a new generation and is always accepted.
 *
 * Returns the *same map reference* when nothing changed. Svelte compares by
 * reference, so this turns a duplicate delivery into zero re-renders rather than
 * a wasted repaint of every table.
 */
export function applyTableState(current: TableMap, next: TableState): TableMap {
  const existing = current.get(next.tableId);
  if (
    existing !== undefined &&
    existing.epoch === next.epoch &&
    next.version <= existing.version
  ) {
    return current;
  }
  const updated = new Map(current);
  updated.set(next.tableId, next);
  return updated;
}

/**
 * Tables in ascending numeric order. Map iteration follows insertion order and
 * retained messages can arrive in any order, so sorting is not optional.
 */
export function sortedTables(tables: TableMap): TableState[] {
  return [...tables.values()].sort((a, b) => a.tableId - b.tableId);
}

export function cookingOrders(table: TableState): Order[] {
  return table.orders.filter((order) => order.status === 'COOKING');
}

export function finishedOrders(table: TableState): Order[] {
  return table.orders.filter((order) => order.status !== 'COOKING');
}

// --- timing ----------------------------------------------------------------

/**
 * Seconds a dish has been cooking, from the server's `placedAt`.
 *
 * `serverOffsetMs` corrects for the browser's clock differing from the kitchen's.
 * Without it, a laptop whose clock is a minute fast would show every dish as
 * having cooked for a minute before it started. Clamped at zero so a bad clock
 * can never render a negative stopwatch.
 */
export function elapsedSeconds(
  placedAt: string,
  nowMs: number,
  serverOffsetMs: number,
): number {
  const placed = Date.parse(placedAt);
  if (Number.isNaN(placed)) return 0;
  const elapsed = nowMs - serverOffsetMs - placed;
  return elapsed > 0 ? Math.floor(elapsed / 1000) : 0;
}

/**
 * Whole seconds remaining until a dish is expected to be ready.
 *
 * Uses `floor`, not `ceil`. `nowMs` comes from a store that ticks once a second,
 * so at the instant an order arrives it can be nearly a full second stale — which
 * makes the computed remainder slightly *larger* than the truth. `ceil` rounded
 * that fraction up and displayed "10s left" for a 9-second cook, every time.
 * `floor` absorbs the sub-second staleness instead of amplifying it, and matches
 * how a countdown is read: "9s left" means at least 9 seconds remain.
 *
 * Clamped at zero. The kitchen's sleep is the authoritative timer, so if it
 * overruns the countdown parks at zero and the UI says "finishing…" rather than
 * going negative or claiming a missed deadline.
 */
export function remainingSeconds(
  expectedReadyAt: string,
  nowMs: number,
  serverOffsetMs: number,
): number {
  const expected = Date.parse(expectedReadyAt);
  if (Number.isNaN(expected)) return 0;
  const remaining = expected - (nowMs - serverOffsetMs);
  return remaining > 0 ? Math.floor(remaining / 1000) : 0;
}

/**
 * How far through its assigned cook time an order is, 0..100.
 *
 * Guards against `cookSeconds` being zero, which tests and a zero-delay config
 * both produce, and which would otherwise divide by zero.
 */
export function progressPercent(
  order: Order,
  nowMs: number,
  serverOffsetMs: number,
): number {
  if (order.cookSeconds <= 0) return 100;
  const elapsed = elapsedSeconds(order.placedAt, nowMs, serverOffsetMs);
  const fraction = elapsed / order.cookSeconds;
  return Math.max(0, Math.min(100, Math.round(fraction * 100)));
}

/**
 * How long a finished dish took.
 *
 * Uses two *server* timestamps, so unlike the live stopwatch this is immune to
 * browser clock skew entirely — no offset needed.
 */
export function cookDurationSeconds(order: Order): number | null {
  if (order.readyAt === null) return null;
  const placed = Date.parse(order.placedAt);
  const ready = Date.parse(order.readyAt);
  if (Number.isNaN(placed) || Number.isNaN(ready)) return null;
  return Math.max(0, Math.round((ready - placed) / 1000));
}

export function formatDuration(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

// --- client-side input validation -----------------------------------------

export interface ValidationResult {
  ok: boolean;
  /** Trimmed value, safe to send. Only meaningful when ok is true. */
  value: string;
  error: string | null;
}

/**
 * Validate a food name in the browser, mirroring the backend's rules.
 *
 * This is a UX affordance, not a security control: the backend re-validates
 * everything, because anything enforced only in a browser can be bypassed by
 * publishing to the broker directly. Checking here just means the customer is
 * told immediately instead of after a round trip.
 */
export function validateFoodName(raw: string, maxLength: number): ValidationResult {
  const value = raw.trim();
  if (value.length === 0) {
    return { ok: false, value, error: 'Please enter what you would like to order.' };
  }
  if (value.length > maxLength) {
    return { ok: false, value, error: `Please keep it under ${maxLength} characters.` };
  }
  if (CONTROL_CHARACTERS.test(value)) {
    return { ok: false, value, error: 'That contains characters we cannot accept.' };
  }
  return { ok: true, value, error: null };
}
