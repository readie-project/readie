// Server-sent events arrive as blank-line separated frames of "event:" and "data:" lines.
function parseFrame(frame: string): {event: string; data: Record<string, unknown>} | null {
  let event = 'message';
  let data = '';
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7);
    else if (line.startsWith('data: ')) data += line.slice(6);
  }
  if (!data) return null;
  try {
    return {event, data: JSON.parse(data)};
  } catch {
    return null;
  }
}


export async function streamExecution(
  serviceUrl: string,
  code: string,
  cold: boolean,
  signal: AbortSignal,
  onEvent: (event: string, data: Record<string, unknown>) => void,
  fetchRequest: typeof fetch = fetch,
): Promise<void> {
  const res = await fetchRequest(`${serviceUrl}/run`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code, disable_optimized_execution: cold, timeout: 30}),
    signal,
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `The playground returned status ${res.status}.`);
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  let completed = false;
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += value;
      let split = buffer.indexOf('\n\n');
      while (split !== -1) {
        const parsed = parseFrame(buffer.slice(0, split));
        buffer = buffer.slice(split + 2);
        split = buffer.indexOf('\n\n');
        if (!parsed) continue;
        onEvent(parsed.event, parsed.data);
        if (parsed.event === 'done') completed = true;
      }
    }
    if (!completed) throw new Error('The execution stream ended before completion.');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
