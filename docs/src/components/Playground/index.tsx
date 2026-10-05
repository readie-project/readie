import {useCallback, useEffect, useRef, useState} from 'react';
import type {KeyboardEvent, ReactNode} from 'react';
import clsx from 'clsx';
import {Highlight} from 'prism-react-renderer';
import {usePrismTheme} from '@docusaurus/theme-common';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';

import {examples} from '../../data/playgroundExamples';
import styles from './styles.module.css';

type LaneId = 'optimized' | 'cold';
type LaneState = {
  output: string;
  running: boolean;
  seconds: number | null;
  error: string;
  truncated: boolean;
};

const idle = (): LaneState => ({output: '', running: false, seconds: null, error: '', truncated: false});
const initial = (): Record<LaneId, LaneState> => ({optimized: idle(), cold: idle()});

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

/* ------------------------------------------------------------------ Editor */

// A textarea over a highlighted copy of its text. The copy uses the same Prism theme as
// every code block on the site, so the editor reads like the rest of the documentation.
function Editor({
  value,
  onChange,
  onRun,
}: {
  value: string;
  onChange: (value: string) => void;
  onRun: () => void;
}): ReactNode {
  const theme = usePrismTheme();
  const releaseTab = useRef(false);

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      onRun();
    } else if (event.key === 'Escape') {
      // Lets the next Tab move focus on instead of indenting, so the editor is not a keyboard trap.
      releaseTab.current = true;
    } else if (event.key === 'Tab' && !event.shiftKey) {
      if (releaseTab.current) {
        releaseTab.current = false;
        return;
      }
      event.preventDefault();
      const el = event.currentTarget;
      const {selectionStart: start, selectionEnd: end} = el;
      onChange(`${value.slice(0, start)}    ${value.slice(end)}`);
      requestAnimationFrame(() => el.setSelectionRange(start + 4, start + 4));
    } else {
      releaseTab.current = false;
    }
  };

  const lineCount = value.split('\n').length;
  return (
      <div className={styles.editor}>
        <pre className={styles.gutter} style={{minWidth: `${String(lineCount).length + 3.5}ch`}} aria-hidden="true">
          {Array.from({length: lineCount}, (_, i) => i + 1).join('\n')}
        </pre>
        <div className={styles.stack}>
          <Highlight theme={theme} code={value} language="python">
            {({tokens, getTokenProps}) => (
              <pre className={styles.highlight} style={{color: theme.plain.color}} aria-hidden="true">
                {tokens.map((line, i) => (
                  <span key={i} className={styles.line}>
                    {line.map((token, k) => (
                      <span key={k} {...getTokenProps({token})} />
                    ))}
                    <br />
                  </span>
                ))}
              </pre>
            )}
          </Highlight>
          <textarea
            className={styles.textarea}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            aria-label="Python code. Press Control Enter to run."
          />
        </div>
      </div>
    );
  }

  /* ------------------------------------------------------------------ Result */

  function useElapsed(running: boolean): number {
    const [elapsed, setElapsed] = useState(0);
    useEffect(() => {
      if (!running) return undefined;
      const start = performance.now();
      setElapsed(0);
      const id = window.setInterval(() => setElapsed((performance.now() - start) / 1000), 100);
      return () => window.clearInterval(id);
    }, [running]);
    return elapsed;
  }

  const seconds = (state: LaneState, elapsed: number): number | null =>
    state.seconds ?? (state.running ? elapsed : null);

  // One run's output, with its name, the color it has in the bar, and its time.
  function Pane({
    title,
    state,
    time,
  }: {
    title: string;
    state: LaneState;
    time: number | null;
  }): ReactNode {
    const empty = !state.output && !state.error && !state.running && state.seconds === null;
    return (
      <section className={styles.pane} aria-label={`${title} output`}>
        <div className={styles.paneHead}>
          <span className={styles.paneName}>{title}</span>
          <span className={styles.paneTime} aria-live="polite">
            {time === null ? '' : `${time.toFixed(2)} s`}
          </span>
        </div>
        <pre className={styles.output}>
          {empty ? <span className={styles.placeholder}>Output appears here.</span> : state.output}
          {state.truncated ? '\n[output truncated]' : ''}
          {state.error ? <span className={styles.error}>{`${state.output ? '\n' : ''}${state.error}`}</span> : null}
        </pre>
      </section>
    );
  }

  function Chevron({direction}: {direction: 'left' | 'right'}): ReactNode {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
        <path
          d={direction === 'left' ? 'M9 2.5 4.5 7 9 11.5' : 'M5 2.5 9.5 7 5 11.5'}
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }

  /* -------------------------------------------------------------------- Page */

  const OWN_CODE = `# Write your own Python here.
  # The standard library and the packages of the examples are available.

  print("Hello from Readie")
  `;

  // The playground: a row of examples and one card with the code and the two results. It is a
  // section of the landing page.
  export default function Playground(): ReactNode {
    const {siteConfig} = useDocusaurusContext();
    const serviceUrl = String(siteConfig.customFields?.playgroundUrl ?? '');
    const [selected, setSelected] = useState(examples[0].id);
    const [code, setCode] = useState(examples[0].code);
    const [own, setOwn] = useState(OWN_CODE);
    const [lanes, setLanes] = useState(initial);
    const [message, setMessage] = useState('');
    const abort = useRef<AbortController | null>(null);
    const track = useRef<HTMLDivElement | null>(null);
    const [edges, setEdges] = useState({start: true, end: false});

    const {optimized, cold} = lanes;
    const running = optimized.running || cold.running;
    const elapsed = useElapsed(running);
    const readieTime = seconds(optimized, elapsed);
    const coldTime = seconds(cold, elapsed);

    const updateEdges = useCallback(() => {
      const el = track.current;
      if (!el) return;
      setEdges({start: el.scrollLeft <= 1, end: el.scrollLeft + el.clientWidth >= el.scrollWidth - 1});
    }, []);

    useEffect(() => {
      updateEdges();
      window.addEventListener('resize', updateEdges);
      return () => window.removeEventListener('resize', updateEdges);
    }, [updateEdges]);

    const slide = (direction: -1 | 1) =>
      track.current?.scrollBy({left: direction * track.current.clientWidth * 0.7, behavior: 'smooth'});

    const patch = useCallback((lane: LaneId, change: Partial<LaneState>) => {
      setLanes((prev) => ({...prev, [lane]: {...prev[lane], ...change}}));
    }, []);

    const edit = (value: string) => {
      setCode(value);
      if (selected === 'own') setOwn(value);
    };

    const run = useCallback(async () => {
      if (!serviceUrl || !code.trim() || abort.current) return;
      setMessage('');
      setLanes({optimized: {...idle(), running: true}, cold: {...idle(), running: true}});
      const controller = new AbortController();
      abort.current = controller;
      try {
        const res = await fetch(`${serviceUrl}/run`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({code}),
          signal: controller.signal,
        });
        if (!res.ok || !res.body) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail ?? `The playground returned status ${res.status}.`);
        }
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = '';
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
            const lane = parsed.data.lane as LaneId;
            if (parsed.event === 'log') {
              setLanes((prev) => ({
                ...prev,
                [lane]: {...prev[lane], output: prev[lane].output + String(parsed.data.text)},
              }));
            } else if (parsed.event === 'done') {
              patch(lane, {
                running: false,
                seconds: Number(parsed.data.duration_s),
                error: String(parsed.data.error ?? ''),
                truncated: Boolean(parsed.data.truncated),
              });
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setMessage((err as Error).message || 'Could not reach the playground.');
        }
      } finally {
        abort.current = null;
        setLanes((prev) => ({
          optimized: {...prev.optimized, running: false},
          cold: {...prev.cold, running: false},
        }));
      }
    }, [code, serviceUrl, patch]);

    const stop = useCallback(() => abort.current?.abort(), []);

    const choose = (id: string) => {
      if (running || id === selected) return;
      const example = examples.find((x) => x.id === id);
      setSelected(id);
      setCode(id === 'own' ? own : (example?.code ?? ''));
      setLanes(initial());
      setMessage('');
    };

    const original = selected === 'own' ? OWN_CODE : (examples.find((x) => x.id === selected)?.code ?? '');
    const changed = code !== original;
    const reset = () => edit(original);

    return (
    <div className={styles.stage}>
      {/* The examples, as a row of thin pills that scrolls sideways. */}
      <div className={styles.carousel}>
        <button
          type="button"
          className={styles.arrow}
          aria-label="Previous examples"
          disabled={edges.start}
          onClick={() => slide(-1)}
        >
          <Chevron direction="left" />
        </button>
        <div className={styles.track} ref={track} onScroll={updateEdges} role="group" aria-label="Examples">
          <button
            type="button"
            className={clsx(styles.pill, styles.own, selected === 'own' && styles.pillOn)}
            aria-pressed={selected === 'own'}
            disabled={running}
            onClick={() => choose('own')}
          >
            Write your own
          </button>
          {examples.map((x) => (
            <button
              key={x.id}
              type="button"
              className={clsx(styles.pill, x.id === selected && styles.pillOn)}
              aria-pressed={x.id === selected}
              disabled={running}
              title={`${x.domain}: ${x.imports}`}
              onClick={() => choose(x.id)}
            >
              {x.title}
            </button>
          ))}
        </div>
        <button
          type="button"
          className={styles.arrow}
          aria-label="Next examples"
          disabled={edges.end}
          onClick={() => slide(1)}
        >
          <Chevron direction="right" />
        </button>
      </div>

      {/* One card, like the live examples on react.dev: a tab bar, then the code on the
          left and the results on the right. */}
      <div className={styles.card}>
        <div className={styles.tabbar}>
          <span className={styles.tab}>main.py</span>
          <span className={styles.actions}>
            <button type="button" className={styles.link} onClick={reset} disabled={running || !changed}>
              Reset
            </button>
            {running ? (
              <button type="button" className={clsx("button button--secondary", styles.run)} onClick={stop}>
                Stop
              </button>
            ) : (
              <button
                type="button"
                className={clsx("button button--primary", styles.run)}
                onClick={run}
                disabled={!serviceUrl || !code.trim()}
                title="Run (Ctrl+Enter)"
              >
                Run
              </button>
            )}
          </span>
        </div>
        <div className={styles.split}>
          <div className={styles.codePane}>
            <Editor value={code} onChange={edit} onRun={run} />
          </div>
          <div className={styles.results}>
            <Pane title="Readie" state={optimized} time={readieTime} />
            <Pane title="Cold start" state={cold} time={coldTime} />
            {!serviceUrl && <p className={styles.notice}>The playground service is not configured for this build.</p>}
            {message && (
              <p className={styles.notice} role="alert">
                {message}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
