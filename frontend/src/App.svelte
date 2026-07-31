<script lang="ts">
  import { onMount } from 'svelte';
  import ConnectionBadge from './lib/components/ConnectionBadge.svelte';
  import OrderDialog from './lib/components/OrderDialog.svelte';
  import TableCard from './lib/components/TableCard.svelte';
  import { createMessagingClient, type MessagingClient } from './lib/messaging';
  import { sortedTables } from './lib/state';
  import {
    connection,
    dismissNotice,
    kitchen,
    notices,
    pushNotice,
    tables,
  } from './lib/stores';

  let client: MessagingClient | null = null;
  let activeTable = $state<number | null>(null);
  let resetting = $state(false);

  onMount(() => {
    // Built through the factory, so the component never names a transport.
    client = createMessagingClient();
    // Returning a teardown from onMount closes the socket when the app unmounts,
    // so a hot reload during development does not pile up broker sessions.
    return () => {
      void client?.disconnect();
      client = null;
    };
  });

  /**
   * The table list is *not* configured in the frontend. The kitchen publishes a
   * retained state message for every table it owns, so the client renders
   * whatever tables it hears about. One source of truth, no config to drift.
   */
  const visibleTables = $derived(sortedTables($tables));

  const brokerConnected = $derived($connection === 'connected');

  /**
   * `null` means we have not heard from the kitchen either way yet — distinct
   * from a kitchen that has told us (or whose Last Will has told us) it is down.
   */
  const kitchenOnline = $derived($kitchen?.status === 'ONLINE');

  /**
   * Tables are hidden while the kitchen is down.
   *
   * The retained state is still the last known truth, so it is not deleted from
   * the store — but rendering it would be a lie: anything shown as COOKING has no
   * process behind it and will never be served. Hiding it is the honest option,
   * and when the kitchen returns it republishes every table so they come straight
   * back.
   */
  const showTables = $derived(kitchenOnline && visibleTables.length > 0);

  /** Ordering needs both a broker *and* a kitchen to receive the order. */
  const canOrder = $derived(brokerConnected && kitchenOnline);

  async function submitOrder(foodName: string): Promise<void> {
    const tableId = activeTable;
    if (tableId === null || client === null) {
      throw new Error('Not connected to the kitchen.');
    }
    await client.publishOrder(tableId, foodName);
    // Only closes on success; a failed publish leaves the dialog open with the
    // text still in it.
    activeTable = null;
  }

  async function resetKitchen(): Promise<void> {
    if (client === null || resetting) return;
    resetting = true;
    try {
      await client.requestReset();
      // No optimistic clearing: the kitchen republishes cleared state, and letting
      // that arrive normally means the UI shows what the server actually did.
      pushNotice('Reset requested. The kitchen is clearing every table.', 'info');
    } catch (cause) {
      pushNotice(
        cause instanceof Error ? cause.message : 'Could not reset the kitchen.',
        'error',
      );
    } finally {
      resetting = false;
    }
  }
</script>

<main>
  <header>
    <h1>Restaurant Orders</h1>
    <div class="header-actions">
      <ConnectionBadge />
      <button class="reset" onclick={resetKitchen} disabled={!canOrder || resetting}>
        {resetting ? 'Resetting…' : 'Reset all'}
      </button>
    </div>
  </header>

  {#if $notices.length > 0}
    <ul class="notices">
      {#each $notices as notice (notice.id)}
        <li class={notice.kind}>
          <span>{notice.text}</span>
          <button onclick={() => dismissNotice(notice.id)} aria-label="Dismiss">x</button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if showTables}
    <div class="tables">
      {#each visibleTables as table (table.tableId)}
        <TableCard {table} disabled={!canOrder} onOrder={(id) => (activeTable = id)} />
      {/each}
    </div>
  {:else if !brokerConnected}
    <p class="waiting">Connecting to the broker…</p>
  {:else if $kitchen === null}
    <!-- Connected, but no status yet. The kitchen's status topic is retained, so
         this state should last only as long as one round trip. -->
    <p class="waiting">Connected. Waiting for the kitchen to announce itself…</p>
  {:else if !kitchenOnline}
    <p class="offline">
      <strong>The kitchen is offline.</strong>
      Tables are hidden because their last known state is no longer live — nothing
      shown as cooking would ever be served. They will reappear on their own when
      the kitchen comes back.
    </p>
  {:else}
    <p class="waiting">The kitchen is online but has not published any tables yet.</p>
  {/if}

  <OrderDialog
    tableId={activeTable}
    onSubmit={submitOrder}
    onCancel={() => (activeTable = null)}
  />
</main>

<style>
  main {
    max-width: 60rem;
    margin: 0 auto;
    padding: 1rem;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }
  h1 {
    font-size: 1.25rem;
    margin: 0;
  }
  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .reset {
    cursor: pointer;
    padding: 0.2rem 0.6rem;
    font-size: 0.85rem;
  }
  .reset:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .waiting {
    color: #666;
  }
  .offline {
    padding: 0.75rem;
    border: 1px solid #c62828;
    border-radius: 4px;
    background: #ffebee;
    color: #b71c1c;
  }
  .tables {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
    gap: 0.75rem;
  }
  .notices {
    list-style: none;
    margin: 0 0 1rem;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .notices li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.4rem 0.6rem;
    border-radius: 4px;
    font-size: 0.85rem;
    border: 1px solid #c62828;
    background: #ffebee;
    color: #b71c1c;
  }
  .notices li.info {
    border-color: #1565c0;
    background: #e3f2fd;
    color: #0d47a1;
  }
  .notices button {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    color: inherit;
  }
</style>
