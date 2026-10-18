<script lang="ts" module>
  import type { Component } from 'svelte';
  /** One tab of the drawer; #326 adds another entry. */
  export interface Tab { id: string; label: string; component: Component<{ name: string }>; }
</script>

<script lang="ts">
  let { name, tabs, onclose }: { name: string; tabs: Tab[]; onclose: () => void } = $props();
  let activeId = $state('');
  const active = $derived(tabs.find((t) => t.id === activeId) ?? tabs[0]);
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape') onclose(); }} />

<aside class="drawer">
  <header>
    <h2>{name}</h2>
    <button type="button" aria-label="Close" onclick={onclose}>x</button>
  </header>
  <nav>
    {#each tabs as t (t.id)}
      <button type="button" class:on={t === active} onclick={() => (activeId = t.id)}>{t.label}</button>
    {/each}
  </nav>
  {#if active}
    {@const Body = active.component}
    <Body {name} />
  {/if}
</aside>

<style>
  .drawer { background: #1b1e26; padding: 8px; overflow: hidden; }
  header { display: flex; justify-content: space-between; align-items: center; }
  h2 { margin: 0; font-size: 16px; }
  nav { display: flex; gap: 4px; margin: 6px 0; }
  nav button.on { font-weight: bold; }
  @media (min-width: 768px) { .drawer { width: 320px; } }
</style>
