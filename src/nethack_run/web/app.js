'use strict';
const $ = id => document.getElementById(id);
let csrf, selected = null, busy = false, lastList = '';
async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options, headers: {'X-CSRF-Token': csrf || '', ...options.headers}});
  if (response.status === 401) { location.assign('/'); throw new Error('Please sign in again'); }
  if (!response.ok) throw new Error(await response.text());
  return response;
}
const name = a => a.character?.name || a.player.model || 'Character creation';
const label = a => a.state === 'finished' ? a.outcome : a.state;
const duration = seconds => `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
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
        const sub = document.createElement('small'); sub.textContent = `${a.player.model || a.player.player_type || 'Unknown player'} · Session ${a.session} · ${a.key_count} keys`;
        b.append(badge, title, sub); b.onclick = () => { selected = a.id; refresh(); }; $('runs').append(b);
      }
    }
    $('detail').hidden = !selected;
    if (selected) {
      const id = selected, a = await (await api('/dashboard/attempts/' + id)).json();
      if (selected !== id) return;
      $('title').textContent = name(a); $('state').textContent = label(a);
      $('metadata').textContent = `${a.character ? [a.character.role, a.character.race, a.character.gender, a.character.align].join(' / ') : 'Choosing a character'} · Session ${a.session} · ${duration(a.session_seconds)} · Last activity ${new Date(a.last_activity * 1000).toLocaleString()}`;
      $('screen').textContent = a.screen || 'This session has ended. Its consumed keys are preserved in the trace.';
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
$('logout').onclick = async () => { try { await api('/auth/logout', {method: 'POST'}); location.assign('/'); } catch (e) { $('error').textContent = e.message; } };
(async () => { try { const me = await (await api('/dashboard/me')).json(); csrf = me.csrf; $('account').textContent = me.login; await refresh(); setInterval(refresh, 1000); } catch (e) { $('error').textContent = e.message; } })();
