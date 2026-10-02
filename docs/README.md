# Readie documentation site

The source of [readie.org](https://readie.org), built with [Docusaurus](https://docusaurus.io/).
It needs Node 20 or newer; the version CI and the maintainers build with is pinned in
[`.nvmrc`](.nvmrc), so `nvm use` (or `fnm use`) in this directory picks it up.

```sh
make docs-install   # npm ci
make docs-start     # live-reloading dev server
make docs-build     # production build; fails on any broken link or anchor
make docs-serve     # build, then serve the result
```

(Equivalently, run `npm ci`, `npm start` and `npm run build` in this directory.)

## Layout

| Path                    | What it holds                                                              |
| ----------------------- | -------------------------------------------------------------------------- |
| `docs/getting-started/` | Install the SDK, run a first function, FAQ                                 |
| `docs/concepts/`        | The ideas behind Readie                                                    |
| `docs/architecture/`    | How the pieces fit together, design decisions, security                    |
| `docs/guides/`          | Tuning calls, fixing errors, glossary - for people using the SDK           |
| `docs/reference/`       | SDK and error reference                                                    |
| `docs/contributing/`    | Developer and operator material: setup, running the stack, deployment      |
| `src/pages/index.tsx`   | The landing page                                                           |
| `tools/`                | Template for the generated protobuf reference                              |

`docs/architecture/protos.md` is **generated** from `protos/*.proto`. After a proto
change run `make docs-tools` once (installs `protoc-gen-doc`), then
`make docs-protos`, and commit the result. CI fails if it is stale.

## Writing style

The pages follow the conventions of the Kubernetes, Google developer and Python
documentation. Each page has one purpose: a concept (explanation), a task, a
reference, or a tutorial.

- Open with a lead paragraph that defines the subject and its role in Readie.
  Do not use a callout box for this.
- Write in the present tense and active voice. Address the reader as "you" only
  for actions. Start steps with an imperative verb and put conditions first.
- Do not use contractions, "we", "let us", "please", or words such as "simply",
  "just", and "easy". Do not use rhetorical questions or "not X, not Y" fragments.
- Use sentence-case headings. Phrase task titles as a verb ("Install the client").
  Questions are allowed only in the FAQ.
- Define a term at first use, or link to the glossary. Use one word per concept:
  sandbox, call, function, worker.
- Put code, identifiers, paths, and environment variables in code font. Tag
  every code fence with a language, and show expected output in a separate fence.
- Use at most two admonitions per page, and only `note` or `warning`.
- Write the product name as "Readie". Environment variables keep their literal
  names, for example `READIE_AUTH_TOKEN`.
- Do not state numbers that change, such as the `alpha` weight. Check claims
  against the code, not only the READMEs. Draw diagrams as Mermaid code fences.

User-facing sections (Get started, Concepts, Guides, Reference) assume the client
uses the hosted service. Development and operator material belongs under
Contribute.

## Publishing

Merging to `main` runs `.github/workflows/docs.yml`, which builds the site and
deploys it to GitHub Pages. `static/CNAME` makes the custom domain survive deploys.
