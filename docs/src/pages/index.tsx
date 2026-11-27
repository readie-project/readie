import type {CSSProperties, ReactNode} from 'react';
import clsx from 'clsx';
import Head from '@docusaurus/Head';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import useBrokenLinks from '@docusaurus/useBrokenLinks';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import CodeBlock from '@theme/CodeBlock';
import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';
import Playground from '../components/Playground';
import Wordmark from '../components/Wordmark';

import styles from './index.module.css';

/* ---------------------------------------------------------------- Timeline */

// A waterfall: one row per startup step, drawn on a shared time scale. Widths are relative
// units that show the shape of the saving, not measured durations, and the page says so.
type Kind = 'start' | 'import' | 'restore' | 'run';
type Step = {label: string; units: number; kind: Kind};

const SCALE = 80;
const SECONDS_PER_UNIT = 0.035;

const coldStart: Step[] = [
  {label: 'Start Python', units: 10, kind: 'start'},
  {label: 'import pandas', units: 20, kind: 'import'},
  {label: 'import sklearn', units: 22, kind: 'import'},
  {label: 'import sklearn.linear_model', units: 14, kind: 'import'},
  {label: 'Run function', units: 14, kind: 'run'},
];

const restored: Step[] = [
  {label: 'Restore checkpoint', units: 14, kind: 'restore'},
  {label: 'Run function', units: 14, kind: 'run'},
];

// A small finish flag: a solid triangular pennant on a pole, tilted slightly to the right.
function FinishFlag(): ReactNode {
  return (
    <svg className={styles.flag} viewBox="0 0 16 16" role="img" aria-label="Finish">
      <title>Finish</title>
      <g transform="rotate(14 4.5 15)">
        <line className={styles.flagPole} x1={4.5} y1={1} x2={4.5} y2={15} />
        <polygon className={styles.flagShape} points="4.5,1.6 13.8,4.9 4.5,8.2" />
      </g>
    </svg>
  );
}

function Group({title, steps}: {title: string; steps: Step[]}): ReactNode {
  let elapsed = 0;
  return (
    <div className={styles.group}>
      <p className={styles.groupTitle}>{title}</p>
      <ul className={styles.steps}>
        {steps.map((step) => {
          const style = {
            '--offset': `${(elapsed / SCALE) * 100}%`,
            '--span': `${(step.units / SCALE) * 100}%`,
            '--delay': `${(elapsed * SECONDS_PER_UNIT).toFixed(2)}s`,
            '--dur': `${(step.units * SECONDS_PER_UNIT).toFixed(2)}s`,
          } as CSSProperties;
          elapsed += step.units;
          return (
            <li key={step.label} className={styles.step} style={style}>
              <span className={styles.stepLabel}>{step.label}</span>
              <span className={styles.track}>
                <span className={clsx(styles.fill, styles[step.kind])} />
                {step.kind === 'run' && <FinishFlag />}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Timeline(): ReactNode {
  return (
    <figure className={styles.timeline}>
      <div className={styles.timelineBody}>
        <Group title="Cold start" steps={coldStart} />
        <Group title="Restored from a checkpoint" steps={restored} />
        <div className={styles.axis} aria-hidden="true">
          <span />
          <span className={styles.axisLine}>time</span>
        </div>
        <figcaption className={styles.caption}>
          Illustrative proportions on one time scale, not measurements.
        </figcaption>
      </div>
    </figure>
  );
}

/* -------------------------------------------------------------------- Page */

const sample = `from readie import remote
import pandas as pd
from sklearn.linear_model import LogisticRegression


@remote
def pass_probability(rows, hours):
    data = pd.DataFrame(rows)
    X, y = data[["hours"]], data["passed"]
    model = LogisticRegression().fit(X, y)

    new = pd.DataFrame({"hours": [hours]})
    return float(model.predict_proba(new)[0, 1])


rows = [
    {"hours": h, "passed": int(h > 3)}
    for h in range(1, 7)
]

print(pass_probability(rows, 3.5))  # about 0.5`;

const sections: [string, string, string][] = [
  ['Get started', '/docs/getting-started/quickstart', 'Install the client and run a first function.'],
  ['Guides', '/docs/guides/http-execution', 'Run code over HTTP or from Claude, tune calls, look up the SDK and its errors, and fix problems.'],
  ['Concepts', '/docs/concepts/cold-starts-and-restore', 'Cold starts, checkpoints, sandboxes and sessions.'],
  ['Architecture', '/docs/architecture/overview', 'The router, workers, executor and pipeline, and how a call travels.'],
  ['Contribute', '/docs/contributing/setup', 'Set up a development environment and run the stack.'],
];


// The first thing a visitor can run: one request, no install. It matches the example in the
// HTTP execution guide, with the JSON body spread over lines and the endpoint last.
const curlExample = `curl -N \\
  -H 'Content-Type: application/json' \\
  -d '{
    "code": "print(\\"Hello from Readie\\")"
  }' \\
  'https://run.readie.org'`;

// The same greeting with the Python client.
const pythonExample = `# pip install readie
from readie import remote


@remote
def say_hello(name):
    return f"Hello from {name}"


print(say_hello("Readie"))`;

function Hero({description}: {description: string}): ReactNode {
  return (
    <header className={styles.hero}>
      <div className={clsx('container', styles.wide, styles.heroInner)}>
        <div className={styles.heroText}>
          {/* The wordmark is the page's visible title, and plays its intro once on load. The
              hidden text gives the heading its name for crawlers and screen readers. */}
          <Heading as="h1" className={styles.title}>
            <span className={styles.visuallyHidden}>Readie</span>
            <Wordmark animated decorative className={styles.wordmark} />
          </Heading>
          <p className={styles.tagline}>{description}</p>
          <div className={styles.actions}>
            <Link className={clsx("button button--primary", styles.cta)} to="/docs/getting-started/quickstart">
              Quickstart
            </Link>
            <Link className={clsx("button button--secondary", styles.cta)} to="/#playground">
              Try Readie
            </Link>
          </div>
        </div>
        {/* Two ways to run code: one request with no install, or the Python client. */}
        <div className={styles.heroCode}>
          <Tabs>
            <TabItem value="http" label="HTTP" default>
              <CodeBlock language="bash">{curlExample}</CodeBlock>
            </TabItem>
            <TabItem value="python" label="Python">
              <CodeBlock language="python">{pythonExample}</CodeBlock>
            </TabItem>
          </Tabs>
        </div>
      </div>
    </header>
  );
}

function HowItWorks(): ReactNode {
  return (
    <section className={clsx(styles.section, styles.band)}>
      <div className={clsx('container', styles.wide)}>
        <div className={styles.intro}>
          <Heading as="h2">How is it better?</Heading>
          <p>
            Importing libraries is most of a serverless cold start. Readie moves that work ahead of
            time. A pipeline starts Python with common packages already imported and saves the
            process as a checkpoint.
          </p>
          <p>
            When a call arrives, the router picks a worker and the checkpoint that holds the
            packages the function needs. The worker restores that checkpoint in a gVisor sandbox
            and runs the function.
          </p>
          <p className={styles.more}>
            <Link to="/docs/architecture/overview">Read about Readie's architecture</Link>
          </p>
        </div>
        <div className={styles.stage}>
          <div className={styles.codeCard}>
            <CodeBlock language="python" title="app.py">
              {sample}
            </CodeBlock>
          </div>
          <Timeline />
        </div>
      </div>
    </section>
  );
}

function TryIt(): ReactNode {
  // The navbar links to this section, so the build must know that the anchor exists.
  useBrokenLinks().collectAnchor('playground');
  return (
    <section id="playground" className={clsx(styles.section, styles.anchored)}>
      <div className={clsx('container', styles.wide)}>
        <div className={clsx(styles.intro, styles.introFull)}>
          <Heading as="h2">Try Readie</Heading>
          <p>Pick an example or write your own, and see the difference.</p>
        </div>
        <Playground />
      </div>
    </section>
  );
}

function Documentation(): ReactNode {
  return (
    <section className={clsx(styles.section, styles.band, styles.last)}>
      <div className={clsx('container', styles.wide)}>
        <Heading as="h2" className={styles.centered}>
          Documentation
        </Heading>
        <div className={styles.cards}>
          {sections.map(([name, to, text]) => (
            <Link key={name} to={to} className={styles.card}>
              <span className={styles.cardTitle}>{name}</span>
              <span className={styles.cardText}>{text}</span>
            </Link>
          ))}
        </div>
      </div>
    </section>
  );
}

// Structured data for search engines: the project, the site, and the Python client.
function structuredData(siteUrl: string, description: string, repoUrl: string): object {
  const home = `${siteUrl}/`;
  return {
    '@context': 'https://schema.org',
    '@graph': [
      {
        '@type': 'Organization',
        '@id': `${home}#organization`,
        name: 'Readie',
        url: home,
        logo: `${siteUrl}/img/logo-512.png`,
        sameAs: [repoUrl, 'https://pypi.org/project/readie'],
      },
      {
        '@type': 'WebSite',
        '@id': `${home}#website`,
        name: 'Readie',
        url: home,
        description,
        inLanguage: 'en',
        publisher: {'@id': `${home}#organization`},
      },
      {
        '@type': 'SoftwareApplication',
        '@id': `${home}#software`,
        name: 'Readie',
        description,
        url: home,
        applicationCategory: 'DeveloperApplication',
        softwareRequirements: 'Python 3.12',
        downloadUrl: 'https://pypi.org/project/readie',
        license: 'https://www.apache.org/licenses/LICENSE-2.0',
        image: `${siteUrl}/img/social-card.png`,
        publisher: {'@id': `${home}#organization`},
      },
    ],
  };
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  const description = siteConfig.tagline;
  const repoUrl = String(siteConfig.customFields?.repoUrl ?? '');
  // The layout puts a given title before the site name, so the home page sets its own: the
  // site name alone.
  const title = siteConfig.title;
  return (
    <Layout description={description} wrapperClassName="landing-page">
      <Head>
        <title>{title}</title>
        <meta property="og:title" content={title} />
        <script type="application/ld+json">
          {JSON.stringify(structuredData(siteConfig.url, description, repoUrl))}
        </script>
      </Head>
      <Hero description={description} />
      <main>
        <HowItWorks />
        <TryIt />
        <Documentation />
      </main>
    </Layout>
  );
}
