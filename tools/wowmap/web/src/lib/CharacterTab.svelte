<script lang="ts">
  import { onMount } from 'svelte';
  import type { components } from '$lib/api';
  import { decisionRow } from '$lib/decisions';

  type Summary = components['schemas']['CharacterSummary'];
  type Decision = components['schemas']['Decision'];

  let { name }: { name: string } = $props();
  let summary = $state<Summary | null>(null);
  let decisions = $state<Decision[] | null>(null);
  let error = $state('');
  let logError = $state('');

  async function get<T>(url: string): Promise<T> {
    const r = await fetch(url);
    if (!r.ok) throw new Error(((await r.json().catch(() => ({}))) as { error?: string }).error ?? `HTTP ${r.status}`);
    return (await r.json()) as T;
  }

  // A slow answer for a character that is no longer open is dropped (`name` is the open one).
  async function load(n: string) {
    const base = `/api/v2/characters/${encodeURIComponent(n)}`;
    await Promise.all([
      get<Summary>(base).then((s) => { if (n === name) { summary = s; error = ''; } },
                              (e) => { if (n === name) error = String(e.message ?? e); }),
      get<{ decisions: Decision[] }>(`${base}/decisions?limit=50`).then((d) => { if (n === name) { decisions = d.decisions; logError = ''; } },
                              (e) => { if (n === name) logError = String(e.message ?? e); })
    ]);
  }

  // Fetched when opened, then every 10 s while open (closing destroys this); another character refetches at once.
  onMount(() => {
    const id = setInterval(() => load(name), 10_000);
    return () => clearInterval(id);
  });
  $effect(() => { summary = null; decisions = null; load(name); });

  const gold = (c: number) => `${Math.floor(c / 10000)}g ${Math.floor(c / 100) % 100}s ${c % 100}c`;
  const when = (t: number) => new Date(t * 1000).toLocaleTimeString();
</script>

<section class="attrs">
  {#if error}<p class="err">{error}</p>{/if}
  {#if summary}
    <p><b style="color:{summary.class_color}">{summary.name}</b> level {summary.level} {summary.race_name} {summary.class_name}</p>
    <dl>
      <dt>Zone</dt><dd>{summary.zone_name}</dd>
      <dt>Status</dt><dd>{summary.online ? 'online' : 'offline'}</dd>
      <dt>Health</dt><dd>{summary.health ?? '?'} / {summary.max_health ?? '?'}</dd>
      <dt>{summary.power_type.replace('_', ' ')}</dt><dd>{summary.power ?? '?'} / {summary.max_power ?? '?'}</dd>
      <dt>Money</dt><dd>{gold(summary.money)}</dd>
    </dl>
  {:else if !error}<p class="none">Loading…</p>{/if}
</section>

<h3>Decisions</h3>
{#if logError}<p class="err">{logError}</p>{/if}
<ul class="log">
  {#each decisions ?? [] as d, i (i)}
    {@const r = decisionRow(d)}
    <li class:failed={r.failed}>
      <time>{when(d.t)}</time> <b>{r.action}</b> <span class="conf">{r.confidence}</span>
      {#if r.failed}<span class="mark">failed</span>{/if}
      {#if r.reason}<div class="reason">{r.reason}</div>{/if}
    </li>
  {:else}
    {#if decisions}<li class="none">No recent decisions.</li>{/if}
  {/each}
</ul>

<style>
  /* Two boxes, each scrolling on its own: the page does not scroll. */
  .attrs { height: 25dvh; overflow-y: auto; }
  .log { list-style: none; margin: 0; padding: 0; height: 35dvh; overflow-y: auto; }
  h3 { margin: 8px 0 4px; font-size: 13px; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; margin: 0; }
  dt { opacity: 0.6; text-transform: capitalize; }
  dd { margin: 0; }
  li { padding: 4px 0; border-bottom: 1px solid #262a33; }
  time { opacity: 0.6; font-size: 12px; }
  .conf { opacity: 0.7; }
  .reason { opacity: 0.7; font-size: 12px; }
  .failed .mark { color: #e07070; font-weight: bold; }
  .err { color: #e07070; }
  .none { opacity: 0.6; }
</style>
