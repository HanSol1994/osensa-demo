<script lang="ts">
  /**
   * Two independent facts, deliberately shown separately:
   *  - `$connection` is *our* link to the broker.
   *  - `$kitchen` is whether the backend is alive, which we only know because the
   *    kitchen publishes a retained status and registers a Last Will.
   * A customer can be perfectly connected to a broker with a dead kitchen, and
   * collapsing the two into one "online" light would hide that.
   */
  import { connection, kitchen } from '../stores';
</script>

<div class="badge">
  <span class="chip" data-state={$connection}>broker: {$connection}</span>
  <span class="chip" data-kitchen={$kitchen?.status ?? 'UNKNOWN'}>
    kitchen: {$kitchen?.status.toLowerCase() ?? 'unknown'}
  </span>
</div>

<style>
  .badge {
    display: flex;
    gap: 0.5rem;
    font-size: 0.85rem;
  }
  .chip {
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    border: 1px solid #bbb;
    background: #f4f4f4;
  }
  .chip[data-state='connected'],
  .chip[data-kitchen='ONLINE'] {
    border-color: #2e7d32;
    background: #e8f5e9;
    color: #1b5e20;
  }
  .chip[data-state='reconnecting'] {
    border-color: #ef6c00;
    background: #fff3e0;
    color: #e65100;
  }
  .chip[data-state='offline'],
  .chip[data-kitchen='OFFLINE'] {
    border-color: #c62828;
    background: #ffebee;
    color: #b71c1c;
  }
</style>
