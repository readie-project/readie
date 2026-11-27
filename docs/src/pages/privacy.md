---
title: Privacy policy
description: How Readie handles the code you run, the requests you send, and visits to readie.org.
---

# Privacy policy

_Last updated: November 26, 2026_

This policy describes how Readie handles information when you use the Readie service, the
`readie` Python client, the Readie plugins for AI agents, or this website. Readie has no
user accounts, does not use cookies or analytics, and does not store the code you run or
its output.

## What this policy covers

- The Readie service, which runs code sent to `api.readie.org` by the Python client, and
  to `run.readie.org` by the HTTP execution endpoint and the MCP server.
- The `readie` Python client and the Readie plugins for AI agents, which send code to the
  service.
- The website at `readie.org`, including the documentation and the playground.

## Code you run

When you run code on Readie, the code, its arguments and its options are sent to the
service over an encrypted connection. The code runs in an isolated sandbox, and its output
and result are sent back to you.

- Readie does not save the code, its arguments, its output or its result after the call
  ends. They exist only in memory and in the sandbox while the call runs.
- A session keeps its Python state in one sandbox between calls. The sandbox and its
  state are removed when the session has been idle for a few minutes.
- Readie does not read, analyze or share the code for any purpose other than running it,
  and does not use it to train models.
- Code that you run can contact other services, for example to install packages from the
  Python Package Index. Those services handle the requests under their own policies.

Do not send passwords, keys or personal data in code that you run on Readie.

## Request logs

The servers that run Readie record basic technical information about each request: the
IP address it came from, the time, the requested path, the response status and the user
agent. For each call, the service also records its request and session identifiers, the
checkpoint and memory it used, the amount of data it returned, and the error message when
the call fails. The logs do not contain the code you send or its output.

Readie uses these logs only to operate the service, to fix problems and to protect it
from abuse. The rate limit holds IP addresses in memory for the duration of its time
window. The logs are kept only as long as they are needed for these purposes and are then
deleted.

## The website

The website is a static site. It sets no cookies and uses no analytics or tracking. It is
hosted on GitHub Pages, and GitHub processes the requests to it as described in the
[GitHub Privacy Statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).

The playground on the website sends the code you enter to `run.readie.org`, where it is
handled as described in [Code you run](#code-you-run).

## AI agents and plugins

The Readie plugins and the MCP server receive only the code and the options that an agent
sends to the `run_python` tool. They do not receive your conversation with the agent. The
company that provides the agent, such as Anthropic or OpenAI, handles your conversation
under its own privacy policy.

## The Python client

The `readie` client sends the functions you call, their arguments and their options to
the service, together with the token in `READIE_AUTH_TOKEN` if you set one. The client
sends no usage data or other telemetry.

## Sharing

Readie does not sell or rent information. Information is shared only with the hosting
providers that run the service on Readie's behalf, or when the law requires it.

## Children

Readie is not directed at children under 13 and does not knowingly collect information
from them.

## Changes to this policy

Changes to this policy are published on this page, and the date at the top changes with
them.

## Contact

For questions about this policy, open an issue in the
[Readie repository](https://github.com/readie-project/readie/issues). To report a privacy
problem privately, use a
[GitHub security advisory](https://github.com/readie-project/readie/security/advisories/new).
