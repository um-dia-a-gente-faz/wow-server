<script lang="ts">
  import { onMount } from 'svelte';
  import type { components } from '$lib/api';

  type Feed = components['schemas']['ActivityResponse'];

  let { name }: { name: string } = $props();
  let feed = $state<Feed | null>(null);
  let error = $state('');

  async function load(n: string) {
    try {
      const r = await fetch(`/api/v2/characters/${encodeURIComponent(n)}/activity?limit=50`);
      if (!r.ok) throw new Error(((await r.json().catch(() => ({}))) as { error?: string }).error ?? `HTTP ${r.status}`);
      const f = (await r.json()) as Feed;
      if (n !== name) return;   // a slow answer for a character that is no longer open
      feed = f;
      error = '';
    } catch (e) {
      if (n === name) error = String(e instanceof Error ? e.message : e);
    }
  }

  // Fetched when opened, then every 10 s while open; a different character refetches at once.
  onMount(() => {
    const id = setInterval(() => load(name), 10_000);
    return () => clearInterval(id);
  });
  $effect(() => { feed = null; load(name); });

  const when = (t: number) => new Date(t * 1000).toLocaleString();
</script>

{#if error}<p class="err">{error}</p>{/if}
<ul class="events">
  {#each feed?.events ?? [] as e, i (i)}
    <li><time>{when(e.t)}</time> <b>{e.kind}</b> {e.text}</li>
  {:else}
    {#if feed}<li class="none">No recent activity.</li>{/if}
  {/each}
</ul>

<style>
  /* Its own scroll box: the page does not scroll. */
  .events { list-style: none; margin: 0; padding: 0; max-height: 50dvh; overflow-y: auto; }
  li { padding: 4px 0; border-bottom: 1px solid #262a33; }
  time { opacity: 0.6; font-size: 12px; margin-right: 4px; }
  .err { color: #e07070; }
  .none { opacity: 0.6; }
</style>
