'use strict';

const $ = id => document.getElementById(id);
let state = {}, busy = true, catalog = {}, statusKey = '', statusError = false, pendingResult = null;
let language = 'en';
let token = location.hash.slice(1);
try {
  language = localStorage.getItem('repaint-align-language') === 'ru' ? 'ru' : 'en';
  if (token) sessionStorage.setItem('repaint-align-token', token);
  else token = sessionStorage.getItem('repaint-align-token') || '';
} catch (_) { /* Storage is optional; the full launch URL still works. */ }
if (location.hash) history.replaceState(null, '', location.pathname);

function tr(key, values = {}) {
  let text = language === 'ru' ? (catalog[key] || key) : key;
  for (const [name, value] of Object.entries(values)) text = text.replaceAll(`{${name}}`, String(value));
  return text;
}
const seconds = x => `${Number(x).toFixed(9)} ${tr('s')}`;
const milliseconds = x => `${x >= 0 ? '+' : ''}${(x * 1000).toFixed(6)} ${tr('ms')}`;
function sampleCount(x) {
  const formatted = Number.isInteger(x) ? String(x) : x.toFixed(3);
  if (language !== 'ru') return `${formatted} ${Math.abs(x) === 1 ? 'sample' : 'samples'}`;
  const kind = new Intl.PluralRules('ru').select(x);
  return `${formatted} ${kind === 'one' ? 'сэмпл' : kind === 'few' ? 'сэмпла' : 'сэмплов'}`;
}
function status(key, error = false) {
  statusKey = key; statusError = error;
  $('status').textContent = tr(key);
  $('status').classList.toggle('error', error);
}
function buttons() {
  document.querySelectorAll('button').forEach(button => { button.disabled = busy; });
  $('analyze').disabled = busy || !state.reference || !state.target;
  $('apply').disabled = busy || !state.result_id;
  document.querySelectorAll('input,select').forEach(element => { element.disabled = busy; });
}
function capture(id, value) {
  const element = $(id);
  element.replaceChildren();
  element.classList.toggle('empty', !value);
  if (!value) { element.textContent = tr('No selection captured yet'); return; }
  const title = document.createElement('strong');
  title.textContent = value.name;
  element.append(title, document.createElement('br'),
    tr('Track {track} · {channels} · {rate} Hz', {track: value.track + 1,
      channels: tr(value.channels === 2 ? 'Stereo' : 'Mono'), rate: value.rate}),
    document.createElement('br'), `${seconds(value.selection_start)} → ${seconds(value.selection_end)}`);
}
function draw(result) {
  const canvas = $('curve'), context = canvas.getContext('2d'), width = canvas.width, height = canvas.height;
  context.clearRect(0, 0, width, height);
  const xs = result.curve_samples, ys = result.curve_ncc;
  const low = Math.min(...ys), high = Math.max(...ys), span = Math.max(high - low, .01);
  context.strokeStyle = '#354158';
  context.beginPath(); context.moveTo(24, height - 24); context.lineTo(width - 24, height - 24); context.stroke();
  context.strokeStyle = '#86adff'; context.lineWidth = 2; context.beginPath();
  xs.forEach((x, index) => {
    const xx = 24 + (xs.length === 1 ? .5 : (x - xs[0]) / (xs.at(-1) - xs[0])) * (width - 48);
    const yy = height - 28 - (ys[index] - low) / span * (height - 52);
    if (index) context.lineTo(xx, yy); else context.moveTo(xx, yy);
  });
  context.stroke(); context.fillStyle = '#bec9db'; context.font = '16px system-ui';
  context.fillText(`${(xs[0] * 1000 / result.sample_rate).toFixed(2)} ${tr('ms')}`, 24, height - 2);
  context.fillText(`${(xs.at(-1) * 1000 / result.sample_rate).toFixed(2)} ${tr('ms')}`, width - 130, height - 2);
  context.fillText(`NCC ${high.toFixed(4)}`, 24, 20);
}
function render(next) {
  state = next;
  capture('reference', state.reference); capture('target', state.target);
  $('result').classList.toggle('hidden', !state.result);
  $('applied').classList.toggle('hidden', !state.applied);
  if (state.applied) $('applied').textContent = tr(
    'Clip moved. Verified start: {start}. Audio samples are preserved. Capture both selections again for another operation.',
    {start: seconds(state.applied.clip_start)});
  if (state.result) {
    const result = state.result;
    $('initial').textContent = milliseconds(result.initial_seconds);
    $('correction').textContent = `${sampleCount(result.correction_samples)} (${milliseconds(result.correction_samples / result.sample_rate)})`;
    $('total').textContent = milliseconds(result.total_seconds);
    const rows = [
      ['Original target position', seconds(state.target.clip_start)],
      ['Initial alignment, samples', (result.initial_seconds * result.sample_rate).toFixed(3)],
      ['Total movement, samples', (result.total_seconds * result.sample_rate).toFixed(3)],
      ['New target position', seconds(state.target.clip_start + result.total_seconds)],
      ['Method / loss', `${result.method} / ${result.loss.toPrecision(6)}`],
      ['Selected candidate NCC', result.ncc.toFixed(6)],
      ['Candidates refined', result.candidates.length],
      ['Fixed comparison interval', sampleCount(result.compared_frames)],
    ];
    $('details').replaceChildren();
    for (const [key, value] of rows) {
      const name = document.createElement('span'), detail = document.createElement('span');
      name.textContent = tr(key); detail.textContent = value; $('details').append(name, detail);
    }
    $('warnings').textContent = result.warnings.map(warning => tr(warning)).join('\n');
    $('candidate_rows').replaceChildren();
    result.candidates.forEach((candidate, index) => {
      const row = document.createElement('tr');
      for (const value of [index + 1, candidate.correction_samples, candidate.ncc.toFixed(6), candidate.loss.toPrecision(6)]) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      $('candidate_rows').append(row);
    });
    draw(result);
  }
  buttons();
}
function translatePage() {
  document.documentElement.lang = language;
  $('language').value = language;
  document.querySelectorAll('[data-i18n]').forEach(element => { element.textContent = tr(element.dataset.i18n); });
  $('curve').setAttribute('aria-label', tr('NCC over correction range'));
  render(state); status(statusKey, statusError);
}
async function request(action, resultId = state.result_id) {
  if (busy) return;
  busy = true; buttons();
  status(action === 'analyze' ? 'Checking captures and analyzing…' : action === 'apply'
    ? 'Checking the project, moving the clip, and verifying audio…' : 'Working with Audacity…');
  try {
    const response = await fetch(`/api/${action}`, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Repaint-Token': token},
      body: JSON.stringify({max_ms: $('max_ms').value, method: $('method').value,
        candidates: $('candidates').value, result_id: resultId}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    render(result);
    status(action === 'analyze' ? 'Analysis complete. Review the result before applying.' : action === 'apply'
      ? 'Position and audio integrity verified.' : action === 'reset'
        ? 'Captures cleared.' : 'Selection captured; the original selection has been restored.');
  } catch (error) {
    // Never offer an old confirmation after a failed request or disconnected browser.
    state.result = state.result_id = null; render(state); status(error.message, true);
  } finally { busy = false; buttons(); }
}
function confirmMovement() {
  if (busy || !state.result_id) return;
  pendingResult = state.result_id;
  $('confirmation_text').textContent = tr(
    'Move the entire clip “{name}” by {shift} to a new start at {start}?',
    {name: state.target.name, shift: milliseconds(state.result.total_seconds),
      start: seconds(state.target.clip_start + state.result.total_seconds)});
  $('confirmation').showModal();
}
document.querySelectorAll('[data-action]').forEach(button => {
  button.addEventListener('click', () => button.dataset.action === 'confirm' ? confirmMovement() : request(button.dataset.action));
});
$('cancel_apply').addEventListener('click', () => { pendingResult = null; $('confirmation').close(); });
$('confirmation').addEventListener('cancel', () => { pendingResult = null; });
$('confirm_apply').addEventListener('click', () => {
  const resultId = pendingResult; pendingResult = null; $('confirmation').close();
  if (resultId) request('apply', resultId);
});
for (const id of ['max_ms', 'method', 'candidates']) $(id).addEventListener('input', () => {
  state.result_id = null; pendingResult = null; buttons();
  if (state.result) status('Settings changed. Analyze again.');
});
$('language').addEventListener('change', () => {
  language = $('language').value;
  try { localStorage.setItem('repaint-align-language', language); } catch (_) { /* Optional. */ }
  translatePage();
});
(async () => {
  try {
    catalog = await (await fetch('/locales/ru.json')).json();
    const response = await fetch('/api/state', {headers: {'X-Repaint-Token': token}});
    const initial = await response.json();
    if (!response.ok) throw new Error(initial.error);
    render(initial);
  } catch (error) { status(error.message, true); }
  finally { busy = false; translatePage(); }
})();
