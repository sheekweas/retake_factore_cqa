// Minimal Chrome DevTools Protocol driver (Node >= 22 global WebSocket, no dependencies).
// Used only by web/dev/ui_check.mjs for browser acceptance checks against the fixture server.
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const CHROME_CANDIDATES = [
  process.env.CQA_CHROME,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium',
].filter(Boolean);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch({ profileDir, port = 9333 }) {
  mkdirSync(profileDir, { recursive: true });
  const { existsSync } = await import('node:fs');
  const exe = CHROME_CANDIDATES.find((p) => existsSync(p));
  if (!exe) throw new Error('Chrome/Edge not found; set CQA_CHROME');
  const proc = spawn(exe, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`, '--window-size=1400,1000', 'about:blank'],
  { stdio: 'ignore' });
  let targets;
  for (let i = 0; i < 50; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); if (targets.length) break; } catch { /* starting */ }
    await sleep(200);
  }
  const page = targets.find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    } else if (msg.method) listeners.forEach((l) => l(msg));
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject }); ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const errors = [];
  listeners.push((m) => {
    if (m.method === 'Runtime.exceptionThrown') errors.push('exception: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push('console.error: ' + m.params.args.map((a) => a.value ?? a.description).join(' '));
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') errors.push('log: ' + m.params.entry.text + ' ' + (m.params.entry.url || ''));
  });
  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  await send('Page.setLifecycleEventsEnabled', { enabled: true });

  const page_ = {
    errors,
    send,
    async goto(url) { await send('Page.navigate', { url }); await sleep(300); await page_.waitFor('document.readyState === "complete"'); },
    async eval(expr) {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error('eval failed: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text) + '\n' + expr);
      return r.result.value;
    },
    async waitFor(expr, timeout = 10000) {
      const t0 = Date.now();
      while (Date.now() - t0 < timeout) {
        try { if (await page_.eval(`Boolean(${expr})`)) return true; } catch { /* retry */ }
        await sleep(100);
      }
      throw new Error('timeout waiting for: ' + expr);
    },
    async screenshot(file) {
      const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
      writeFileSync(file, Buffer.from(r.data, 'base64'));
    },
    async close() { try { await send('Browser.close'); } catch { /* closed */ } ws.close(); proc.kill(); },
  };
  return page_;
}

export { sleep, join };
