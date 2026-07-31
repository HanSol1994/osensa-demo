/**
 * The frontend half of docs/EVENT-CONTRACT.md.
 *
 * TypeScript types vanish at runtime, so every inbound MQTT payload is checked
 * by a hand-written guard before it is allowed near the UI. A `TableState`
 * arriving from the network is untrusted input exactly like an HTTP response
 * body: casting it with `as TableState` would be a lie that surfaces later as an
 * undefined-property crash in a component.
 *
 * These shapes are duplicated from the pydantic models rather than generated.
 * That is a deliberate, documented trade-off — see the README; generating both
 * sides from one JSON Schema is the improvement to make with more time.
 */

/** Mirrors MAX_FOOD_NAME_LENGTH in the backend. Kept in sync by hand. */
export const MAX_FOOD_NAME_LENGTH = 80;

export const ORDER_STATUSES = ['COOKING', 'SERVED', 'FAILED'] as const;
export type OrderStatus = (typeof ORDER_STATUSES)[number];

export const REJECTION_REASONS = [
  'VALIDATION_FAILED',
  'UNKNOWN_TABLE',
  'TABLE_AT_CAPACITY',
  'KITCHEN_AT_CAPACITY',
] as const;
export type RejectionReason = (typeof REJECTION_REASONS)[number];

export interface Order {
  orderId: string;
  foodName: string;
  status: OrderStatus;
  placedAt: string;
  /**
   * The random cook time the kitchen assigned, in whole seconds.
   *
   * **Display only.** The authoritative timer is the backend's `asyncio.sleep`;
   * an order becomes SERVED only when the kitchen publishes that. Ignoring or
   * tampering with this value cannot make a dish arrive sooner or later.
   */
  cookSeconds: number;
  /** `placedAt + cookSeconds`, computed server-side. Drives the countdown. */
  expectedReadyAt: string;
  readyAt: string | null;
}

export interface TableState {
  tableId: number;
  /**
   * The kitchen generation that produced this state. Because kitchen state is
   * in-memory, a restart resets `version` to zero; comparing versions across
   * epochs would make a client reject the restarted kitchen forever.
   */
  epoch: string;
  version: number;
  updatedAt: string;
  orders: Order[];
}

export interface OrderRejected {
  clientOrderId: string | null;
  tableId: number | null;
  reason: RejectionReason;
  message: string;
}

export interface KitchenStatus {
  status: 'ONLINE' | 'OFFLINE';
  since: string;
}

/** Outbound. The only message this app publishes. */
export interface OrderPlaced {
  clientOrderId: string;
  clientId: string;
  tableId: number;
  foodName: string;
  sentAt: string;
}

// --- runtime validation ----------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isFiniteInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value);
}

function parseOrder(raw: unknown): Order | null {
  if (!isRecord(raw)) return null;
  const { orderId, foodName, status, placedAt, cookSeconds, expectedReadyAt, readyAt } =
    raw;
  if (typeof orderId !== 'string' || orderId.length === 0) return null;
  if (typeof foodName !== 'string') return null;
  if (typeof status !== 'string') return null;
  if (!(ORDER_STATUSES as readonly string[]).includes(status)) return null;
  if (typeof placedAt !== 'string') return null;
  // Number.isInteger also rejects NaN and Infinity, either of which would poison
  // the countdown arithmetic and render "NaN" on screen. Requiring an integer
  // keeps the client honest about the contract rather than quietly rounding a
  // float the backend should never have sent.
  if (typeof cookSeconds !== 'number' || !Number.isInteger(cookSeconds)) return null;
  if (typeof expectedReadyAt !== 'string') return null;
  if (readyAt !== null && typeof readyAt !== 'string') return null;
  return {
    orderId,
    foodName,
    status: status as OrderStatus,
    placedAt,
    cookSeconds,
    expectedReadyAt,
    readyAt: readyAt ?? null,
  };
}

/** Returns null for anything that is not a well-formed TableState. */
export function parseTableState(raw: unknown): TableState | null {
  if (!isRecord(raw)) return null;
  const { tableId, epoch, version, updatedAt, orders } = raw;
  if (!isFiniteInteger(tableId)) return null;
  if (typeof epoch !== 'string' || epoch.length === 0) return null;
  if (!isFiniteInteger(version)) return null;
  if (typeof updatedAt !== 'string') return null;
  if (!Array.isArray(orders)) return null;

  const parsed: Order[] = [];
  for (const candidate of orders) {
    const order = parseOrder(candidate);
    // One malformed order invalidates the whole snapshot rather than being
    // skipped: a partially applied table is worse than a rejected update,
    // because the retained message will resend the correct one.
    if (order === null) return null;
    parsed.push(order);
  }
  return { tableId, epoch, version, updatedAt, orders: parsed };
}

export function parseOrderRejected(raw: unknown): OrderRejected | null {
  if (!isRecord(raw)) return null;
  const { clientOrderId, tableId, reason, message } = raw;
  if (typeof reason !== 'string') return null;
  if (!(REJECTION_REASONS as readonly string[]).includes(reason)) return null;
  if (typeof message !== 'string') return null;
  if (clientOrderId !== null && typeof clientOrderId !== 'string') return null;
  if (tableId !== null && !isFiniteInteger(tableId)) return null;
  return {
    clientOrderId: clientOrderId ?? null,
    tableId: tableId ?? null,
    reason: reason as RejectionReason,
    message,
  };
}

export function parseKitchenStatus(raw: unknown): KitchenStatus | null {
  if (!isRecord(raw)) return null;
  const { status, since } = raw;
  if (status !== 'ONLINE' && status !== 'OFFLINE') return null;
  if (typeof since !== 'string') return null;
  return { status, since };
}

/** Parse JSON without throwing; malformed bytes are a dropped message, not a crash. */
export function safeJsonParse(payload: string): unknown {
  try {
    return JSON.parse(payload);
  } catch {
    return undefined;
  }
}
