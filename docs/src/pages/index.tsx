import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import CodeBlock from '@theme/CodeBlock';

import styles from './index.module.css';

const sample = `from readie import remote


@remote
def add(a, b):
    return a + b


print(add(1, 2))  # runs in a remote sandbox`;

type Step = {title: string; body: ReactNode};

const steps: Step[] = [
  {
    title: '1. Build checkpoints offline',
    body: (
      <>
        A pipeline starts Python with common packages already imported and saves the live
        process as a <em>checkpoint</em>. The import cost is paid once, before any call arrives.
      </>
    ),
  },
  {
    title: '2. Select a checkpoint',
    body: (
      <>
        For each <code>@remote</code> call, the <em>router</em> selects a worker and the
        checkpoint that already holds the packages the function needs.
      </>
    ),
  },
  {
    title: '3. Restore and run',
    body: (
      <>
        The <em>worker</em> restores the checkpoint in a gVisor sandbox, runs the function, and
        returns the result to the caller.
      </>
    ),
  },
];

type Card = {title: string; to: string; body: string};

const cards: Card[] = [
  {
    title: 'Quickstart',
    to: '/docs/getting-started/quickstart',
    body: 'Install the SDK and run a first remote function.',
  },
  {
    title: 'Concepts',
    to: '/docs/concepts/cold-starts-and-restore',
    body: 'Cold starts, checkpoints, sandboxes and sessions.',
  },
  {
    title: 'Architecture',
    to: '/docs/architecture/overview',
    body: 'The router, workers, executor and pipeline, and the path of a call between them.',
  },
  {
    title: 'Troubleshooting',
    to: '/docs/guides/troubleshooting',
    body: 'Error messages with their causes and fixes.',
  },
  {
    title: 'Reference',
    to: '/docs/reference/sdk',
    body: 'SDK options and exceptions.',
  },
  {
    title: 'Contribute',
    to: '/docs/contributing/setup',
    body: 'Development setup, running the stack, and submitting changes.',
  },
];

function Hero(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero', styles.hero)}>
      <div className="container">
        <div className={styles.heroGrid}>
          <div>
            <Heading as="h1" className={styles.title}>
              {siteConfig.title}
            </Heading>
            <p className={styles.tagline}>{siteConfig.tagline}</p>
            <p className={styles.lead}>
              Readie runs a Python function decorated with <code>@remote</code> in an isolated
              sandbox. The sandbox is restored from a checkpoint that already has the required
              libraries imported, which removes most of the cold-start time.
            </p>
            <div className={styles.buttons}>
              <Link className="button button--primary button--lg" to="/docs/getting-started/quickstart">
                Quickstart
              </Link>
              <Link className="button button--secondary button--lg" to="/docs/architecture/overview">
                How it works
              </Link>
            </div>
          </div>
          <div className={styles.code}>
            <CodeBlock language="python">{sample}</CodeBlock>
          </div>
        </div>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout title="Documentation" description={siteConfig.tagline}>
      <Hero />
      <main>
        <section className={styles.section}>
          <div className="container">
            <Heading as="h2">How it works</Heading>
            <div className={styles.steps}>
              {steps.map((s) => (
                <div key={s.title} className={styles.step}>
                  <Heading as="h3">{s.title}</Heading>
                  <p>{s.body}</p>
                </div>
              ))}
            </div>
            <p>
              For the full sequence, see <Link to="/docs/architecture/request-lifecycle">Request lifecycle</Link>.
            </p>
          </div>
        </section>

        <section className={clsx(styles.section, styles.alt)}>
          <div className="container">
            <Heading as="h2">Documentation</Heading>
            <div className={styles.cards}>
              {cards.map((c) => (
                <Link key={c.title} to={c.to} className={styles.card}>
                  <Heading as="h3">{c.title}</Heading>
                  <p>{c.body}</p>
                </Link>
              ))}
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
