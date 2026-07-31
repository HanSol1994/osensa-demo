<script lang="ts">
  /**
   * A native `<dialog>` rather than a hand-rolled overlay: the browser gives us
   * focus trapping, Escape-to-close and correct accessibility semantics for free.
   */
  import { validateFoodName } from '../state';
  import { MAX_FOOD_NAME_LENGTH } from '../types';

  interface Props {
    /** The table being ordered for, or null when the dialog should be closed. */
    tableId: number | null;
    onSubmit: (foodName: string) => Promise<void>;
    onCancel: () => void;
  }

  let { tableId, onSubmit, onCancel }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let value = $state('');
  let error = $state<string | null>(null);
  let submitting = $state(false);

  // Drives the imperative dialog API from declarative state, so the parent only
  // has to set `tableId` and never touches the DOM.
  $effect(() => {
    if (dialog === null) return;
    if (tableId !== null && !dialog.open) {
      value = '';
      error = null;
      submitting = false;
      dialog.showModal();
    } else if (tableId === null && dialog.open) {
      dialog.close();
    }
  });

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    // Validated here purely so the customer gets instant feedback; the backend
    // enforces the same rules because a browser check proves nothing.
    const result = validateFoodName(value, MAX_FOOD_NAME_LENGTH);
    if (!result.ok) {
      error = result.error;
      return;
    }

    submitting = true;
    try {
      await onSubmit(result.value);
    } catch (cause) {
      // Keep the dialog open with the text intact so the order is not lost.
      error = cause instanceof Error ? cause.message : 'Could not send the order.';
      submitting = false;
      return;
    }
    submitting = false;
  }
</script>

<dialog bind:this={dialog} onclose={onCancel}>
  <form onsubmit={handleSubmit}>
    <h2>Order for table {tableId ?? ''}</h2>

    <label for="food-name">What would you like to order?</label>
    <input
      id="food-name"
      type="text"
      bind:value
      maxlength={MAX_FOOD_NAME_LENGTH}
      autocomplete="off"
      disabled={submitting}
      placeholder="e.g. Pad Thai"
    />

    {#if error !== null}
      <p class="error" role="alert">{error}</p>
    {/if}

    <menu>
      <button type="button" onclick={onCancel} disabled={submitting}>Cancel</button>
      <button type="submit" disabled={submitting}>
        {submitting ? 'Sending…' : 'Submit'}
      </button>
    </menu>
  </form>
</dialog>

<style>
  dialog {
    border: 1px solid #999;
    border-radius: 8px;
    padding: 1rem;
    min-width: 18rem;
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 35%);
  }
  h2 {
    margin: 0 0 0.75rem;
    font-size: 1rem;
  }
  label {
    display: block;
    margin-bottom: 0.25rem;
    font-size: 0.85rem;
  }
  input {
    width: 100%;
    padding: 0.4rem;
    box-sizing: border-box;
  }
  .error {
    color: #b71c1c;
    font-size: 0.85rem;
    margin: 0.5rem 0 0;
  }
  menu {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin: 1rem 0 0;
    padding: 0;
  }
  button {
    cursor: pointer;
    padding: 0.35rem 0.8rem;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
</style>
