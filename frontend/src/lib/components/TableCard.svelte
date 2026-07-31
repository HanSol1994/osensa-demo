<script lang="ts">
  import { nowMs } from '../clock';
  import {
    cookDurationSeconds,
    cookingOrders,
    finishedOrders,
    formatDuration,
    progressPercent,
    remainingSeconds,
  } from '../state';
  import { serverOffsetMs } from '../stores';
  import type { TableState } from '../types';

  interface Props {
    table: TableState;
    onOrder: (tableId: number) => void;
    disabled?: boolean;
  }

  let { table, onOrder, disabled = false }: Props = $props();

  // `$derived` recomputes only when `table` changes. Because the store reducer
  // returns the same object for a duplicate delivery, an unchanged table costs
  // nothing here.
  const cooking = $derived(cookingOrders(table));
  const finished = $derived(finishedOrders(table));
</script>

<section class="table">
  <header>
    <h2>Table {table.tableId}</h2>
    <button onclick={() => onOrder(table.tableId)} {disabled}>ORDER</button>
  </header>

  <div class="plate">
    {#if cooking.length === 0 && finished.length === 0}
      <p class="empty">No orders yet.</p>
    {/if}

    <!-- Keyed by orderId so Svelte moves existing DOM nodes instead of
         recreating them when an order changes status. -->
    {#each finished as order (order.orderId)}
      {@const took = cookDurationSeconds(order)}
      <div class="item served" class:failed={order.status === 'FAILED'}>
        <span aria-hidden="true">{order.status === 'FAILED' ? '!' : '*'}</span>
        <span class="name">{order.foodName}</span>
        <!-- Derived from two server timestamps, so this figure is unaffected by
             the browser's clock being wrong. -->
        <small>
          {order.status === 'FAILED' ? 'failed' : 'served'}
          {#if took !== null}· took {formatDuration(took)}{/if}
        </small>
      </div>
    {/each}

    {#each cooking as order (order.orderId)}
      <!-- $nowMs ticks once a second from one shared interval; reading it here is
           what makes the countdown live. Both figures are display only — the
           kitchen's own timer decides when this dish is actually served. -->
      {@const left = remainingSeconds(order.expectedReadyAt, $nowMs, $serverOffsetMs)}
      {@const progress = progressPercent(order, $nowMs, $serverOffsetMs)}
      <div class="item cooking">
        <span aria-hidden="true">~</span>
        <span class="name">{order.foodName}</span>
        <small class="timer">
          {#if left > 0}{formatDuration(left)} left{:else}finishing…{/if}
          <!-- The kitchen's assigned random cook time, shown outright. -->
          <span class="assigned">of {order.cookSeconds}s</span>
        </small>
      </div>
      <div
        class="progress"
        role="progressbar"
        aria-valuenow={progress}
        aria-valuemin="0"
        aria-valuemax="100"
        aria-label="Cooking {order.foodName}"
      >
        <div class="progress-fill" style="width: {progress}%"></div>
      </div>
    {/each}
  </div>
</section>

<style>
  .table {
    border: 1px solid #ccc;
    border-radius: 8px;
    padding: 0.75rem;
    background: #fff;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }
  h2 {
    font-size: 1rem;
    margin: 0;
  }
  button {
    cursor: pointer;
    padding: 0.3rem 0.75rem;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .plate {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-height: 4rem;
  }
  .empty {
    margin: 0;
    color: #888;
    font-size: 0.85rem;
  }
  .item {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    font-size: 0.9rem;
  }
  .item .name {
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .item small {
    color: #666;
  }
  .timer {
    /* Tabular figures stop the row from shifting as the digits change. */
    font-variant-numeric: tabular-nums;
  }
  .assigned {
    color: #999;
  }
  .progress {
    height: 3px;
    background: #e0e0e0;
    border-radius: 2px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: #ef6c00;
    /* Matches the 1s tick so the bar glides instead of stepping. */
    transition: width 1s linear;
  }
  .cooking {
    opacity: 0.75;
  }
  .served .name {
    color: #1b5e20;
  }
  .failed .name {
    color: #b71c1c;
  }
</style>
