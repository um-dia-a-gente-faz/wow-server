<script lang="ts">
  import { onMount } from 'svelte';
  import L from 'leaflet';
  import 'leaflet/dist/leaflet.css';
  import type { components } from '$lib/api';
  import type { Area, AreasResponse } from '$lib/areas';
  import CharacterDrawer, { type Tab } from '$lib/CharacterDrawer.svelte';
  import ActivityTab from '$lib/ActivityTab.svelte';
  import { lastSeenText } from '$lib/lastseen';
  import { SHEET_H, SHEET_W, MAP_FRAME_H, MAP_FRAME_W, ll, markerXY } from '$lib/position';

  type Player = components['schemas']['Player'];

  let areas = $state<Area[]>([]);
  let continents = $state<Area[]>([]);
  let area = $state<Area | null>(null);
  let players = $state<Player[]>([]);
  let reconnecting = $state(false);
  let mapEl: HTMLDivElement;
  let map: L.Map;
  let art: L.ImageOverlay | null = null;
  let selected = $state<string | null>(null);
  let now = $state(Math.floor(Date.now() / 1000));
  const tabs: Tab[] = [{ id: 'activity', label: 'Activity', component: ActivityTab }];
  const markers = new Map<string, L.Marker>();

  const FRAME = L.latLngBounds(ll(0, MAP_FRAME_H), ll(MAP_FRAME_W, 0));
  const key = (a: Area) => String(a.area_id);
  const here = $derived(
    area ? players.flatMap((p) => { const xy = markerXY(p, area!); return xy ? [{ p, xy }] : []; }) : []
  );

  function pick(id: string) {
    area = [...continents, ...areas].find((a) => key(a) === id) ?? null;
  }

  onMount(() => {
    map = L.map(mapEl, {
      crs: L.CRS.Simple, attributionControl: false, zoomSnap: 0,
      maxBounds: FRAME.pad(0.5), maxBoundsViscosity: 0.8
    });
    map.fitBounds(FRAME);

    fetch('/api/areas').then((r) => r.json()).then((d: AreasResponse) => {
      areas = d.areas;
      continents = d.continents;
      const busy = d.in_use[0];
      area = d.areas.find((a) => a.area_id === busy?.zone) ?? d.continents[0] ?? d.areas[0] ?? null;
    });

    const tick = setInterval(() => { now = Math.floor(Date.now() / 1000); }, 30_000);
    const es = new EventSource('/api/v2/stream');
    es.addEventListener('players', (e) => {
      players = (JSON.parse((e as MessageEvent).data) as { players: Player[] }).players;
      reconnecting = false;
    });
    es.onerror = () => { reconnecting = true; };   // EventSource reconnects by itself
    return () => { clearInterval(tick); es.close(); map.remove(); };
  });

  // Zone art: re-laid and re-fitted only when the zone changes, never on a player update.
  $effect(() => {
    if (!map) return;
    art?.remove();
    art = null;
    if (area?.has_image) {
      art = L.imageOverlay('/maps/' + area.image, [ll(0, SHEET_H), ll(SHEET_W, 0)]).addTo(map);
      map.fitBounds(FRAME);
    }
  });

  $effect(() => {
    if (!map) return;
    const seen = new Set<string>();
    for (const { p, xy } of here) {
      seen.add(p.name);
      const at = ll(...xy);
      let m = markers.get(p.name);
      if (!m) {
        m = L.marker(at, { keyboard: false }).addTo(map);
        const name = p.name;
        m.on('click', () => { selected = name; });
        markers.set(p.name, m);
      } else {
        m.setLatLng(at);
      }
      m.setIcon(L.divIcon({
        className: p.online ? 'marker' : 'marker offline', iconSize: undefined,
        html: `<i style="background:${esc(p.class_color)}"></i><b>${esc(p.name)} ${p.level}</b>`
      }));
    }
    for (const [name, m] of markers) if (!seen.has(name)) { m.remove(); markers.delete(name); }
  });

  const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
</script>

<main>
  <div class="mapbox">
    <div class="bar">
      <select value={area ? key(area) : ''} onchange={(e) => pick(e.currentTarget.value)}>
        <optgroup label="Continents">
          {#each continents as c (key(c))}<option value={key(c)}>{c.name}</option>{/each}
        </optgroup>
        <optgroup label="Zones">
          {#each areas as a (key(a))}<option value={key(a)}>{a.name}</option>{/each}
        </optgroup>
      </select>
      {#if reconnecting}<span class="badge">reconnecting</span>{/if}
    </div>
    <div class="map" bind:this={mapEl}></div>
  </div>
  <details class="list" open>
    <summary>Players here ({here.length})</summary>
    <ul>
      {#each here as { p } (p.name)}
        <li class:offline={!p.online}>
          <button type="button" class="row" onclick={() => (selected = p.name)}>
            <i style="background:{p.class_color}"></i>{p.name} <small>{p.level} {p.class_name}</small>
            {#if !p.online && p.last_seen}<small>offline · last seen {lastSeenText(p.last_seen, now)}</small>{/if}
          </button>
        </li>
      {/each}
    </ul>
  </details>
  {#if selected}
    <CharacterDrawer name={selected} {tabs} onclose={() => (selected = null)} />
  {/if}
</main>

<style>
  :global(body) { margin: 0; background: #14161c; color: #e6e6e6; font: 14px system-ui, sans-serif; }
  main { display: flex; flex-direction: column; height: 100dvh; }
  .mapbox { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .bar { display: flex; gap: 8px; align-items: center; padding: 6px; }
  .map { flex: 1; min-height: 0; background: #0c0d11; }
  .badge { background: #8a5a00; border-radius: 4px; padding: 2px 6px; font-size: 12px; }
  .list { max-height: 35dvh; overflow: auto; padding: 6px; background: #1b1e26; }
  ul { list-style: none; margin: 4px 0 0; padding: 0; }
  li { padding: 3px 0; }
  i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
  .row { all: unset; cursor: pointer; display: block; }
  li.offline { opacity: 0.45; }
  :global(.marker.offline) { opacity: 0.45; }
  small { opacity: 0.6; }
  :global(.marker) { white-space: nowrap; width: 0; height: 0; }
  :global(.marker i) { position: absolute; left: -5px; top: -5px; border: 1px solid #000; margin: 0; }
  :global(.marker b) { position: absolute; left: 8px; top: -8px; font-size: 11px; color: #fff; text-shadow: 0 0 3px #000, 0 0 3px #000; }
  @media (min-width: 768px) {
    main { flex-direction: row; }
    .list { width: 260px; max-height: none; }
  }
</style>
