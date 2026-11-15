'use strict';
const $ = id => document.getElementById(id);
let csrf, selected = null, busy = false, lastList = '';
let replayId = null, replayPlayer = null, replayEpoch = 0;
async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options, headers: {'X-CSRF-Token': csrf || '', ...options.headers}});
  if (response.status === 401) { location.assign('/'); throw new Error('Please sign in again'); }
  if (!response.ok) throw new Error(await response.text());
  return response;
}
const name = a => a.character?.name || a.player.model || 'Character creation';
const label = a => a.state === 'finished' ? a.outcome : a.state;
const playerLabel = a => {
  const player = a.player.model || a.player.player_type || 'Unknown player';
  return a.player.player_type === 'model' || a.player.model || a.player.reasoning_effort
    ? `${player} · Reasoning: ${a.player.reasoning_effort || 'unknown'}` : player;
};
const duration = seconds => `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
function showReplay(attempt) {
  const id = attempt?.state === 'finished' ? attempt.id : null;
  if (id === replayId) return;
  const epoch = ++replayEpoch;
  replayId = id;
  if (replayPlayer) { replayPlayer.dispose(); replayPlayer = null; }
  $('replay-player').replaceChildren();
  $('replay-section').hidden = !id;
  $('cast-download').hidden = $('replay-retry').hidden = true;
  if (!id) return;
  $('replay-status').textContent = 'Preparing recording from the recorded moves…';
  (async () => {
    try {
      while (epoch === replayEpoch && selected === id) {
        const response = await api(`/dashboard/attempts/${id}/replay`, {method: 'POST'});
        const result = await response.json();
        if (epoch !== replayEpoch || selected !== id) return;
        if (result.status === 'ready') {
          replayPlayer = AsciinemaPlayer.create(result.url, $('replay-player'), {preload: true, idleTimeLimit: 2, fit: 'width'});
          replayPlayer.addEventListener('error', () => { $('replay-status').textContent = 'The recording could not be loaded.'; $('replay-retry').hidden = false; });
          $('replay-status').textContent = 'Ready to replay.';
          $('cast-download').href = result.url + '?download=1';
          $('cast-download').hidden = false;
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    } catch (e) {
      if (epoch === replayEpoch) { $('replay-status').textContent = e.message; $('replay-retry').hidden = false; }
    }
  })();
}
async function refresh() {
  if (busy) return;
  try {
    const all = await (await api('/dashboard/attempts')).json();
    const filter = $('filter').value;
    const runs = all.filter(a => filter === 'all' || (filter === 'active' ? a.state !== 'finished' : a.state === 'finished'));
    $('count').textContent = `${runs.length} ${runs.length === 1 ? 'attempt' : 'attempts'}`;
    if (!runs.some(a => a.id === selected)) selected = runs[0]?.id || null;
    const signature = JSON.stringify(runs.map(a => [a.id, name(a), label(a), a.session, a.key_count, a.player])) + selected;
    if (signature !== lastList) {
      lastList = signature;
      $('runs').replaceChildren();
      if (!runs.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = 'No attempts here yet. Games created through the API will appear automatically.'; $('runs').append(p); }
      for (const a of runs) {
        const b = document.createElement('button'); b.className = 'run' + (a.id === selected ? ' selected' : ''); b.setAttribute('aria-pressed', String(a.id === selected));
        const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = label(a);
        const title = document.createElement('strong'); title.textContent = name(a);
        const sub = document.createElement('small'); sub.textContent = `${playerLabel(a)} · Session ${a.session} · ${a.key_count} keys`;
        b.append(badge, title, sub); b.onclick = () => { selected = a.id; refresh(); }; $('runs').append(b);
      }
    }
    $('detail').hidden = !selected;
    if (!selected) showReplay(null);
    if (selected) {
      const id = selected, a = await (await api('/dashboard/attempts/' + id)).json();
      if (selected !== id) return;
      $('title').textContent = name(a); $('state').textContent = label(a);
      $('metadata').textContent = `${playerLabel(a)} · ${a.character ? [a.character.role, a.character.race, a.character.gender, a.character.align].join(' / ') : 'Choosing a character'} · Session ${a.session} · ${duration(a.session_seconds)} · Last activity ${new Date(a.last_activity * 1000).toLocaleString()}`;
      $('screen').textContent = a.screen || 'This session has ended. Its consumed keys are preserved in the trace.';
      $('screen').hidden = a.state === 'finished';
      showReplay(a);
      $('pause').hidden = !['running', 'advancing'].includes(a.state);
      $('resume').hidden = !['paused', 'saved'].includes(a.state);
      $('download').hidden = $('package-note').hidden = a.state !== 'finished';
      $('result').textContent = a.result ? `Score ${a.result.score} · ${a.result.turns} turns · Depth ${a.result.maximum_depth}\n${a.result.cause_of_death}` : '';
      $('identifier').textContent = a.id;
    }
    $('error').textContent = '';
  } catch (e) { $('error').textContent = e.message; }
}
async function action(kind) {
  if (!selected || busy) return;
  busy = true;
  try {
    const response = await api(`/dashboard/attempts/${selected}/${kind}`, {method: 'POST'});
    if (kind === 'package') {
      const url = URL.createObjectURL(await response.blob()), link = document.createElement('a');
      link.href = url; link.download = `${selected}.tar`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    $('error').textContent = '';
  } catch (e) { $('error').textContent = e.message; }
  finally { busy = false; }
}
$('pause').onclick = () => action('pause'); $('resume').onclick = () => action('resume'); $('download').onclick = () => action('package');
$('filter').onchange = refresh;
$('replay-retry').onclick = () => { replayId = null; refresh(); };
$('logout').onclick = async () => { try { await api('/auth/logout', {method: 'POST'}); location.assign('/'); } catch (e) { $('error').textContent = e.message; } };
(async () => { try { const me = await (await api('/dashboard/me')).json(); csrf = me.csrf; $('account').textContent = me.login; await refresh(); setInterval(refresh, 1000); } catch (e) { $('error').textContent = e.message; } })();
