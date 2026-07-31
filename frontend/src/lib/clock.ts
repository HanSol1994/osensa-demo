/**
 * A single shared ticking clock.
 *
 * One interval for the whole app rather than one per cooking order. Svelte's
 * readable store takes a start/stop notifier: the function runs when the first
 * subscriber arrives and its return value runs when the last one leaves, so the
 * interval exists only while something is actually displaying a timer. That is
 * the idiomatic way to own a resource in a store without leaking it.
 */

import { readable } from 'svelte/store';

export const TICK_MS = 1000;

export const nowMs = readable(Date.now(), (set) => {
  // Publish immediately rather than waiting a full tick. The stop notifier clears
  // the interval when the last subscriber leaves, and the store then holds that
  // final value — which can be minutes old by the time a new timer appears. A
  // stale "now" makes elapsed time compute as negative, so the first render after
  // re-subscribing would show 0s until the next tick corrected it.
  set(Date.now());
  const handle = setInterval(() => set(Date.now()), TICK_MS);
  return () => clearInterval(handle);
});
