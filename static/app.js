'use strict';
const $ = id => document.getElementById(id);
const storageKey = 'pdf-normalizer-job-v1';
let config, job, currentFile, busy = false, pollTimer, lastAnnounced;
const bytes = n => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} GiB` : n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} MiB` : `${Math.max(1, Math.round(n / 1024))} KiB`;
function error(message = '') { $('error').textContent = message; $('error').hidden = !message; }
function save() { try { job ? localStorage.setItem(storageKey, JSON.stringify(job)) : localStorage.removeItem(storageKey); } catch {} }
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (job?.token) headers.set('Authorization', `Bearer ${job.token}`);
  const response = await fetch(`/api${path}`, { ...options, headers });
  const result = await response.json();
  if (!response.ok) { const e = new Error(typeof result.detail === 'string' ? result.detail : 'The request could not be completed. Please try again.'); e.status = response.status; e.result = result; throw e; }
  return result;
}
function render(row = job) {
  $('upload-view').hidden = !!row; $('job-view').hidden = !row;
  if (!row) return;
  $('file-name').textContent = row.name;
  $('file-size').textContent = bytes(row.size);
  const state = row.status, isDone = state === 'done', terminal = ['done', 'error', 'cancelled'].includes(state);
  $('state-pill').textContent = ({ uploading: busy ? 'Uploading' : 'Paused', queued: 'In queue', processing: 'Rebuilding', verifying: 'Checking', done: 'Ready', error: 'Stopped', cancelled: 'Cancelled' })[state] || state;
  const headings = { uploading: busy ? 'Uploading your document' : 'Ready to resume your upload', queued: 'Your document is in the queue', processing: 'Rebuilding every page', verifying: 'Checking the finished document', done: 'Your clean copy is ready', error: 'We couldn’t finish this document', cancelled: 'Your files have been deleted' };
  $('job-heading').textContent = headings[state] || state;
  let percent = state === 'uploading' ? row.uploaded / row.size * 100 : (row.total ? row.completed / row.total * 100 : 0);
  if (isDone) percent = 100;
  if (state === 'queued') $('progress').removeAttribute('value'); else $('progress').value = percent;
  $('progress-label').textContent = ['processing', 'verifying'].includes(state) ? `${row.completed} / ${row.total || '…'} pages` : state === 'uploading' ? `${Math.floor(percent)}%` : isDone ? bytes(row.output_size) : '';
  const details = { uploading: busy ? 'Keep this tab open while uploading. An interrupted upload can be resumed.' : 'Choose the same file to continue from the last completed chunk.', queued: `Position ${row.queue_position || 1} in the queue. You can close this tab and return on this browser.`, processing: 'Your upload is saved. You can close this tab and return on this browser.', verifying: 'Checking that every page is a single image with no text or vector layers.', done: `Available until ${new Date(row.expires * 1000).toLocaleString()}. Your original upload has been deleted.`, error: row.error || 'Please try another PDF.', cancelled: 'Choose another PDF whenever you’re ready.' };
  $('job-detail').textContent = details[state];
  $('download-button').hidden = !isDone;
  if (isDone) $('download-button').href = `/api/jobs/${row.id}/download?token=${encodeURIComponent(row.token)}`;
  $('resume-button').hidden = state !== 'uploading' || busy;
  $('cancel-button').hidden = state === 'cancelled';
  $('cancel-button').textContent = terminal ? 'Delete files' : 'Cancel & delete';
  $('new-button').hidden = !terminal;
  const rank = { uploading: 0, queued: 1, processing: 1, verifying: 2, done: 3 }[state] ?? -1;
  document.querySelectorAll('.stages li').forEach((el, i) => { el.classList.toggle('active', i === rank); el.classList.toggle('complete', i < rank); });
  if (lastAnnounced !== state) { $('announcer').textContent = headings[state]; lastAnnounced = state; }
}
function update(row) { job = { ...job, ...row }; save(); render(); }
function schedulePoll() { clearTimeout(pollTimer); if (job && !['done', 'error', 'cancelled', 'uploading'].includes(job.status)) pollTimer = setTimeout(poll, 1600); }
async function poll() {
  if (!job) return;
  const id = job.id;
  try { const row = await api(`/jobs/${id}`); if (job?.id !== id) return; update(row); error(); }
  catch (e) { if ([404, 410].includes(e.status)) { update({ status: 'error', error: 'This file is no longer available. Upload it again to create a new copy.' }); } else error('Connection interrupted. We’ll keep checking your saved job.'); }
  schedulePoll();
}
async function upload(file) {
  if (busy) return;
  busy = true; currentFile = file; error(); render();
  const id = job.id;
  try {
    let row = await api(`/jobs/${id}`); update(row);
    let attempts = 0;
    while (job?.id === id && job.status === 'uploading' && job.uploaded < file.size) {
      const offset = job.uploaded;
      try {
        const result = await api(`/jobs/${id}/upload`, { method: 'PATCH', headers: { 'Upload-Offset': String(offset), 'Content-Type': 'application/octet-stream' }, body: file.slice(offset, Math.min(offset + config.chunk_bytes, file.size)) });
        if (job?.id !== id || job.status !== 'uploading') return;
        update(result); attempts = 0;
      } catch (e) {
        if (e.status === 409 && Number.isInteger(e.result.uploaded)) { update({ uploaded: e.result.uploaded }); continue; }
        if (e.status || ++attempts > 3) throw e;
        await new Promise(resolve => setTimeout(resolve, attempts * 1000));
        row = await api(`/jobs/${id}`); if (job?.id !== id || job.status !== 'uploading') return; update(row);
      }
    }
    if (job?.id === id && job.status === 'uploading') { update(await api(`/jobs/${id}/complete`, { method: 'POST', body: '' })); schedulePoll(); }
  } catch (e) { if (job?.id === id && job.status !== 'cancelled') error(e.message || 'Upload paused. Choose the same file to resume.'); }
  finally { busy = false; currentFile = null; render(); }
}
async function choose(file) {
  if (!file || busy) return;
  error();
  if (!config) return error('The service is still connecting. Please try again in a moment.');
  if (!file.name.toLowerCase().endsWith('.pdf')) return error('Choose a PDF file.');
  if (!file.size) return error('This file is empty. Choose another PDF.');
  if (file.size > config.max_upload_bytes) return error(`The current upload limit is ${bytes(config.max_upload_bytes)}. Split this PDF into smaller sets first.`);
  if (job?.status === 'uploading') {
    if (file.name !== job.name || file.size !== job.size || (job.lastModified && file.lastModified !== job.lastModified)) return error('Choose the same, unchanged PDF to resume, or cancel this upload first.');
    return upload(file);
  }
  if (job && !['done', 'error', 'cancelled'].includes(job.status)) return;
  $('choose-button').disabled = true;
  try { job = await api('/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: file.name, size: file.size }) }); job.lastModified = file.lastModified; save(); await upload(file); }
  catch (e) { error(e.message); }
  finally { $('choose-button').disabled = false; }
}
$('choose-button').onclick = $('resume-button').onclick = () => { $('file-input').value = ''; $('file-input').click(); };
$('file-input').onchange = e => choose(e.target.files[0]);
$('new-button').onclick = () => { clearTimeout(pollTimer); job = null; save(); error(); render(); $('announcer').textContent = ''; lastAnnounced = null; $('choose-button').focus(); };
$('cancel-button').onclick = async () => {
  $('cancel-button').disabled = true;
  try { const row = await api(`/jobs/${job.id}/cancel`, { method: 'POST', body: '' }); update(row); clearTimeout(pollTimer); error(); }
  catch (e) { error(e.message); }
  finally { $('cancel-button').disabled = false; }
};
for (const event of ['dragenter', 'dragover']) $('dropzone').addEventListener(event, e => { e.preventDefault(); $('dropzone').classList.add('dragging'); });
for (const event of ['dragleave', 'drop']) $('dropzone').addEventListener(event, e => { e.preventDefault(); $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', e => { if (e.dataTransfer.files.length !== 1) return error('Choose one PDF at a time.'); choose(e.dataTransfer.files[0]); });
window.addEventListener('beforeunload', e => { if (busy) { e.preventDefault(); e.returnValue = ''; } });
(async () => {
  try { config = await api('/config'); $('limits').textContent = `Up to ${bytes(config.max_upload_bytes)} per PDF · Resumable uploads`; $('retention').textContent = config.retention_hours; }
  catch { $('limits').textContent = 'Service unavailable'; error('We couldn’t connect to the processing service. Refresh to try again.'); }
  try { const saved = JSON.parse(localStorage.getItem(storageKey)); if (saved?.id && saved?.token) { job = saved; render(); await poll(); } } catch { localStorage.removeItem(storageKey); }
})();
