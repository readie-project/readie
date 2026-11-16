const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const source = fs.readFileSync(`${__dirname}/../src/utils/streamExecution.ts`, 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
}).outputText;
const sandbox = {exports: {}, fetch, TextDecoderStream};
vm.runInNewContext(compiled, sandbox);
const {streamExecution} = sandbox.exports;
const done = 'event: done\ndata: {"ok":true,"duration_s":1}\n\n';

test('comparison requests select independent execution modes and handle split frames', async () => {
  const requests = [];
  const received = [[], []];
  const mockFetch = async (url, init) => {
    requests.push({url, body: JSON.parse(init.body), signal: init.signal});
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({start(controller) {
      const frames = 'event: log\ndata: {"text":"hello"}\n\n' + done;
      for (let i = 0; i < frames.length; i += 7) controller.enqueue(encoder.encode(frames.slice(i, i + 7)));
      controller.close();
    }}));
  };
  const controller = new AbortController();
  await Promise.all([false, true].map((cold, index) => streamExecution(
    '/playground', 'print(1)', cold, controller.signal,
    (event, data) => received[index].push({event, data}), mockFetch,
  )));
  assert.deepEqual(requests.map(r => r.body), [
    {code: 'print(1)', disable_optimized_execution: false, timeout: 30},
    {code: 'print(1)', disable_optimized_execution: true, timeout: 30},
  ]);
  assert.ok(requests.every(r => r.url === '/playground/run' && r.signal === controller.signal));
  assert.ok(received.every(events => events.length === 2 && events[0].event === 'log' && events[1].event === 'done'));
});

test('one rejected lane does not prevent the other request completing', async () => {
  const mockFetch = async (_, init) => JSON.parse(init.body).disable_optimized_execution
    ? new Response(done) : Response.json({detail: 'busy'}, {status: 503});
  const results = await Promise.allSettled([false, true].map(cold => streamExecution(
    '/playground', 'print(1)', cold, new AbortController().signal, () => {}, mockFetch,
  )));
  assert.equal(results[0].status, 'rejected');
  assert.equal(results[0].reason.message, 'busy');
  assert.equal(results[1].status, 'fulfilled');
});

test('the shared stop signal cancels both requests', async () => {
  let cancelled = 0;
  const mockFetch = (_, init) => new Promise((resolve, reject) => {
    init.signal.addEventListener('abort', () => {cancelled++; reject(new DOMException('Stopped', 'AbortError'));});
  });
  const controller = new AbortController();
  const requests = [false, true].map(cold => streamExecution(
    '/playground', 'print(1)', cold, controller.signal, () => {}, mockFetch,
  ));
  controller.abort();
  const results = await Promise.allSettled(requests);
  assert.equal(cancelled, 2);
  assert.ok(results.every(r => r.status === 'rejected' && r.reason.name === 'AbortError'));
});

test('an interrupted stream without completion is an error', async () => {
  await assert.rejects(streamExecution('/playground', 'x', false,
    new AbortController().signal, () => {}, async () => new Response('')),
    /ended before completion/);
});
