import {useEffect, useState} from 'react';
import type {ReactNode} from 'react';
import clsx from 'clsx';
import CodeBlock from '@theme/CodeBlock';

import styles from './styles.module.css';

const START_DELAY_MS = 300;
const CHARACTER_MS = 26;
const LINE_PAUSE_MS = 280;
// How long the finished command stays on screen, and the pause on the empty block, before the
// next round.
const HOLD_MS = 5000;
const RESTART_MS = 250;

type Props = {
  /** The command to type. */
  children: string;
  /** The start of the command, shown from the first frame and kept when the typing restarts. */
  prefix?: string;
  language?: string;
};

/**
 * A code block that types its text out, holds the finished command, and starts again.
 *
 * The block that a visitor can use is the complete one underneath: its text is transparent, and
 * its copy button always copies the whole command. The typed text is drawn on top of it and
 * takes no clicks.
 */
export default function TypedCommand({
  children: text,
  prefix = '',
  language = 'bash',
}: Props): ReactNode {
  const start = text.startsWith(prefix) ? prefix.length : 0;
  const [count, setCount] = useState(start);
  const done = count >= text.length;

  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setCount(text.length);
      return undefined;
    }
    let timer: ReturnType<typeof setTimeout>;
    const type = (next: number) => {
      setCount(next);
      if (next >= text.length) {
        timer = setTimeout(() => {
          setCount(start);
          timer = setTimeout(() => type(start + 1), RESTART_MS);
        }, HOLD_MS);
        return;
      }
      // A short pause after a line break, and a little jitter, so that it reads as typing.
      const pause = text[next - 1] === '\n' ? LINE_PAUSE_MS : CHARACTER_MS + Math.random() * 22;
      timer = setTimeout(() => type(next + 1), pause);
    };
    timer = setTimeout(() => type(start + 1), START_DELAY_MS);
    return () => clearTimeout(timer);
  }, [text, start]);

  return (
    <div className={clsx(styles.typed, !done && styles.typing)}>
      {/* The complete block. Its text is transparent, so it holds the final size, and it keeps
          the copy button and the selectable text. */}
      <div className={styles.ghost}>
        <CodeBlock language={language}>{text}</CodeBlock>
      </div>
      {/* The typed text, on top. It is a picture of the command, so assistive technology skips it. */}
      <div className={styles.live} aria-hidden="true">
        {count > 0 && <CodeBlock language={language}>{text.slice(0, count)}</CodeBlock>}
      </div>
    </div>
  );
}
