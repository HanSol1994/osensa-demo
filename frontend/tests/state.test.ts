import { describe, expect, it } from 'vitest';
import {
  applyTableState,
  cookDurationSeconds,
  elapsedSeconds,
  emptyTableMap,
  formatDuration,
  progressPercent,
  remainingSeconds,
  sortedTables,
  validateFoodName,
} from '../src/lib/state';
import { MAX_FOOD_NAME_LENGTH, type Order, type TableState } from '../src/lib/types';

const PLACED_AT = '2026-01-01T00:00:00.000Z';
const PLACED_MS = Date.parse(PLACED_AT);

function order(overrides: Partial<Order> = {}): Order {
  return {
    orderId: 'o1',
    foodName: 'Ramen',
    status: 'COOKING',
    placedAt: PLACED_AT,
    cookSeconds: 9,
    expectedReadyAt: new Date(PLACED_MS + 9000).toISOString(),
    readyAt: null,
    ...overrides,
  };
}

function table(overrides: Partial<TableState> = {}): TableState {
  return {
    tableId: 1,
    epoch: 'epoch-a',
    version: 1,
    updatedAt: PLACED_AT,
    orders: [],
    ...overrides,
  };
}

describe('remainingSeconds', () => {
  it('does not overstate the remaining time when the clock store is stale', () => {
    // Regression test. `nowMs` comes from a store that ticks once a second, so at
    // the instant an order arrives it can be almost a full second behind real
    // time, making the computed remainder slightly LARGER than the truth. With
    // Math.ceil that fraction rounded up and a 9-second cook rendered as
    // "10s left" every single time.
    const staleNow = PLACED_MS - 819;
    expect(remainingSeconds(order().expectedReadyAt, staleNow, 0)).toBe(9);
  });

  it('reports the full duration at the moment of placing', () => {
    expect(remainingSeconds(order().expectedReadyAt, PLACED_MS, 0)).toBe(9);
  });

  it('counts down one second at a time', () => {
    const expected = order().expectedReadyAt;
    expect(remainingSeconds(expected, PLACED_MS + 1000, 0)).toBe(8);
    expect(remainingSeconds(expected, PLACED_MS + 5000, 0)).toBe(4);
    expect(remainingSeconds(expected, PLACED_MS + 8999, 0)).toBe(0);
  });

  it('clamps to zero when the kitchen overruns instead of going negative', () => {
    expect(remainingSeconds(order().expectedReadyAt, PLACED_MS + 30_000, 0)).toBe(0);
  });

  it('subtracts the server clock offset', () => {
    // Browser clock running 5s ahead of the kitchen: without correction the dish
    // would appear 5 seconds further along than it is.
    expect(remainingSeconds(order().expectedReadyAt, PLACED_MS + 5000, 5000)).toBe(9);
  });

  it('returns zero for an unparseable timestamp rather than NaN', () => {
    expect(remainingSeconds('not-a-date', PLACED_MS, 0)).toBe(0);
  });
});

describe('elapsedSeconds', () => {
  it('counts up from placedAt', () => {
    expect(elapsedSeconds(PLACED_AT, PLACED_MS + 3500, 0)).toBe(3);
  });

  it('never goes negative even if the browser clock is behind', () => {
    expect(elapsedSeconds(PLACED_AT, PLACED_MS - 10_000, 0)).toBe(0);
  });
});

describe('progressPercent', () => {
  it('tracks progress through the assigned cook time', () => {
    expect(progressPercent(order(), PLACED_MS, 0)).toBe(0);
    expect(progressPercent(order(), PLACED_MS + 4500, 0)).toBe(44);
    expect(progressPercent(order(), PLACED_MS + 9000, 0)).toBe(100);
  });

  it('cannot exceed 100 when the kitchen overruns', () => {
    expect(progressPercent(order(), PLACED_MS + 60_000, 0)).toBe(100);
  });

  it('treats a zero-second cook as complete rather than dividing by zero', () => {
    expect(progressPercent(order({ cookSeconds: 0 }), PLACED_MS, 0)).toBe(100);
  });
});

describe('cookDurationSeconds', () => {
  it('uses two server timestamps, so it needs no clock correction', () => {
    const served = order({
      status: 'SERVED',
      readyAt: new Date(PLACED_MS + 8600).toISOString(),
    });
    expect(cookDurationSeconds(served)).toBe(9);
  });

  it('is null while still cooking', () => {
    expect(cookDurationSeconds(order())).toBeNull();
  });
});

describe('formatDuration', () => {
  it('uses seconds below a minute and m:ss above', () => {
    expect(formatDuration(0)).toBe('0s');
    expect(formatDuration(59)).toBe('59s');
    expect(formatDuration(60)).toBe('1:00');
    expect(formatDuration(125)).toBe('2:05');
  });
});

describe('applyTableState', () => {
  it('accepts a newer version', () => {
    const first = applyTableState(emptyTableMap, table({ version: 1 }));
    const second = applyTableState(first, table({ version: 2 }));
    expect(second.get(1)?.version).toBe(2);
  });

  it('ignores a duplicate delivery and returns the same reference', () => {
    // Reference equality matters: Svelte compares by reference, so an unchanged
    // map means zero re-renders rather than a wasted repaint of every table.
    const first = applyTableState(emptyTableMap, table({ version: 5 }));
    const again = applyTableState(first, table({ version: 5 }));
    expect(again).toBe(first);
  });

  it('ignores an out-of-order older version', () => {
    const first = applyTableState(emptyTableMap, table({ version: 5 }));
    const older = applyTableState(first, table({ version: 4 }));
    expect(older).toBe(first);
    expect(older.get(1)?.version).toBe(5);
  });

  it('accepts a lower version from a new epoch', () => {
    // The kitchen restarted (or was reset), so versions began again at zero. A
    // plain version check would reject this forever and leave dead orders on
    // screen permanently.
    const first = applyTableState(emptyTableMap, table({ epoch: 'epoch-a', version: 5 }));
    const restarted = applyTableState(
      first,
      table({ epoch: 'epoch-b', version: 0, orders: [] }),
    );
    expect(restarted).not.toBe(first);
    expect(restarted.get(1)?.epoch).toBe('epoch-b');
    expect(restarted.get(1)?.version).toBe(0);
  });

  it('keeps tables independent', () => {
    const withOne = applyTableState(emptyTableMap, table({ tableId: 1, version: 3 }));
    const withTwo = applyTableState(withOne, table({ tableId: 2, version: 1 }));
    expect(withTwo.size).toBe(2);
    expect(withTwo.get(1)?.version).toBe(3);
  });
});

describe('sortedTables', () => {
  it('sorts numerically regardless of arrival order', () => {
    // Retained messages arrive in whatever order the broker sends them, and Map
    // iteration follows insertion order, so sorting is not optional.
    let map = emptyTableMap;
    for (const id of [3, 1, 4, 2]) {
      map = applyTableState(map, table({ tableId: id }));
    }
    expect(sortedTables(map).map((t) => t.tableId)).toEqual([1, 2, 3, 4]);
  });
});

describe('validateFoodName', () => {
  it('accepts a normal name and trims it', () => {
    const result = validateFoodName('  Pad Thai  ', MAX_FOOD_NAME_LENGTH);
    expect(result.ok).toBe(true);
    expect(result.value).toBe('Pad Thai');
  });

  it('accepts names containing spaces', () => {
    // Guards a real bug: an earlier control-character regex was written as a
    // character *range* from space to hyphen, which rejected every name with a
    // space in it.
    expect(validateFoodName('Green curry with rice', MAX_FOOD_NAME_LENGTH).ok).toBe(true);
  });

  it('rejects blank input', () => {
    expect(validateFoodName('   ', MAX_FOOD_NAME_LENGTH).ok).toBe(false);
  });

  it('rejects anything longer than the limit', () => {
    expect(validateFoodName('x'.repeat(81), MAX_FOOD_NAME_LENGTH).ok).toBe(false);
    expect(validateFoodName('x'.repeat(80), MAX_FOOD_NAME_LENGTH).ok).toBe(true);
  });

  it('rejects control characters, which could forge a log line', () => {
    expect(validateFoodName('Ramen\nFAKE LOG ENTRY', MAX_FOOD_NAME_LENGTH).ok).toBe(
      false,
    );
    // Built with fromCharCode rather than escape literals so this source file
    // stays plain ASCII and no invisible byte can hide in the repository.
    for (const code of [0, 27, 31, 127]) {
      const name = 'Ramen' + String.fromCharCode(code);
      expect(validateFoodName(name, MAX_FOOD_NAME_LENGTH).ok).toBe(false);
    }
  });
});
