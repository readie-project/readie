---
title: Use Readie in Claude
sidebar_position: 6
description: Connect Claude chat to Readie, or install the Readie plugin for Claude Code, to run Python on Readie from Claude and other AI agents.
---

Readie connects to Claude through the Readie MCP server, a hosted [Model Context Protocol](https://modelcontextprotocol.io) server at `https://run.readie.org/mcp`. With Readie connected, Claude runs Python code on Readie and reads the output. In Claude chat (claude.ai, the desktop app, and the mobile apps), Readie is a connector. In Claude Code, the coding assistant that runs in a terminal or an editor, the Readie plugin adds the connector together with commands that add the `@remote` decorator to functions in a project and explain Readie errors. Running code requires no account, no token, and no local installation.

## Connect Claude chat

1. In Claude, open **Customize**, then **Connectors**.
2. Select **Add**, then **Add custom connector**.
3. Enter `Readie` as the name and `https://run.readie.org/mcp` as the URL, then select **Continue**.
4. Under authentication, select **No sign-in**, then select **Add**.
5. If a **Connect** button appears, select it.

In a Team or Enterprise organization, an Owner adds the connector under **Organization settings**, then **Connectors**, and members then connect it.

Claude uses the connector when a conversation asks to run code on Readie. Claude chat limits how long one tool call can run, so a long execution can fail in chat even though Readie is still running it. For long executions, use the [Python client](/docs/getting-started/quickstart) or Claude Code.

## Install the plugin in Claude Code

1. Add the Readie marketplace in Claude Code:

   ```text
   /plugin marketplace add readie-project/plugins
   ```

2. Install the plugin:

   ```text
   /plugin install readie@readie
   ```

3. Restart Claude Code, or run `/reload-plugins`.

## Run code

Ask Claude to run code on Readie, for example "Run this script on Readie with 4 GiB of memory". Claude calls the `run_python` tool and reports the printed output, the duration, and any error.

In Claude Code, the `run` command runs a file or a snippet directly:

```text
/readie:run analysis.py --memory 4Gi --packages polars==1.9.0
```

The code runs as a script, so only the output that it prints is returned. The `run_python` tool accepts the same options as `@remote`, which the [SDK reference](/docs/guides/sdk) describes: `packages`, `memory`, `max_memory`, `gpu`, `gpu_memory`, `max_gpu_memory`, `timeout`, `session`, `disable_optimized_execution`, and `checkpoint_id`. The [limits of the HTTP execution endpoint](/docs/guides/http-execution#limits) also apply to `run_python`, and the two share one request budget per client address.

To keep Python state between runs, ask Claude to use a session. The `new_session` tool creates a random session identifier, and the runs that share it share the state of one sandbox. See [Sessions](/docs/concepts/sessions).

:::warning[Code leaves your machine]
The `run_python` tool sends the code to the Readie service. Local files and local modules are not available to it. Do not run code that contains secrets.
:::

## Move a function to Readie

In Claude Code, the `offload` command adds `@remote` to a function in the current project:

```text
/readie:offload resize_images
```

Claude Code first checks that the local Python version is 3.12 and that the `readie` package is installed. It then decorates the function. Next, it sets `packages`, `memory`, or `gpu` from what the function does. Finally, it reports the objects that cannot be pickled and the imports of local modules, both of which fail on a remote worker. See [Packages and resources](/docs/getting-started/packages-and-resources) for these limits.

The plugin also loads its Readie instructions automatically when a conversation mentions Readie, `@remote`, or a Readie exception. Claude Code then follows the rules on this site when it writes code that uses the client, and it uses the [errors reference](/docs/guides/errors) to explain failures.

## Connect other agents

Any agent that supports remote MCP servers over streamable HTTP can use the `run_python` and `new_session` tools. Add a server with this URL:

```text
https://run.readie.org/mcp
```

The server requires no authentication. In Claude Code without the plugin, add it with this command:

```bash
claude mcp add --transport http readie https://run.readie.org/mcp
```

## Related pages

- [Run code over HTTP](/docs/guides/http-execution): the HTTP endpoint, which runs code with the same options and limits.
- [SDK reference](/docs/guides/sdk): the arguments of `@remote`.
- [Troubleshooting](/docs/guides/troubleshooting): causes and fixes for common errors.
- [Privacy policy](/privacy): what Readie does with the code that it runs.
