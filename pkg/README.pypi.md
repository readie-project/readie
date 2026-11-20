[![Readie | Secure sandboxes with optimized startup and import times for AI agents and running Python code remotely](https://raw.githubusercontent.com/readie-project/readie/main/assets/banner-wordmark-light.png)](https://readie.org)

[![PyPI](https://img.shields.io/pypi/v/readie)](https://pypi.org/project/readie)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](https://readie.org/docs/getting-started/installation)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/readie-project/readie/blob/main/LICENSE)

[Readie](https://readie.org) runs Python functions remotely with minimal startup and
import times.
Each call runs in a [gVisor sandbox](https://readie.org/docs/concepts/sandboxes)
restored from a [checkpoint](https://readie.org/docs/concepts/checkpoints): a saved
Python process that has already imported a chosen set of packages, so the call
does not start Python or import those packages again.

- **Fast startup.** A restore replaces interpreter startup and library imports,
  which are often the largest part of a cold start. See
  [Cold starts and restore](https://readie.org/docs/concepts/cold-starts-and-restore).
- **Built for Python.** Decorate a function with `@remote` and call it. The `readie`
  client finds the imports of the function, and Readie picks the checkpoint that
  fits them best.
- **Isolated and secure.** By default, every call runs in a fresh gVisor sandbox,
  which keeps untrusted code away from the host and from other calls.
- **Packages and GPUs.** Install extra PyPI packages per call, and set memory and
  GPU budgets on the decorator.
- **Sessions.** Keep a sandbox and its Python state between calls with a
  [session](https://readie.org/docs/concepts/sessions).

## Documentation

The documentation is at [readie.org](https://readie.org):

- [Quickstart](https://readie.org/docs/getting-started/quickstart): run a first function.
- [Packages and resources](https://readie.org/docs/getting-started/packages-and-resources): install PyPI packages per call, and set memory and GPU budgets.
- [Sessions and errors](https://readie.org/docs/getting-started/sessions-and-errors): keep state between calls, and handle exceptions.
- [SDK reference](https://readie.org/docs/guides/sdk): `remote`, `Client`, `Session`, `Settings`, and `configure`.
- [Errors](https://readie.org/docs/guides/errors) and [Troubleshooting](https://readie.org/docs/guides/troubleshooting): exceptions, their causes, and fixes.

## Install the client

The `readie` package is the Python client for Readie. It requires Python 3.12,
because a pickled function is portable only within one Python minor version and
the remote sandbox runs Python 3.12.

```bash
pip install readie
```

## Run a function

Decorate a function with `@remote` and call it:

```python
from readie import remote


@remote
def add(a, b):
    print("Hello from Readie")
    return a + b


print(add(1, 2))
```

Example output:

```text
Python version: 3.12.x (main, ...)
cloudpickle version: 3.1.x
Hello from Readie
Execution completed in ...
3
```

The client serializes the function with `cloudpickle`, and Readie runs the
function in a sandbox. On every call, the client prints the Python and
cloudpickle versions before the call and the time of the round trip after it.
`Hello from Readie` is the output of the function, which the client prints as it
arrives from the sandbox. In async code, call `await add.aio(1, 2)` instead.

## Contribute

The source is on [GitHub](https://github.com/readie-project/readie). To build
and test it, see [Set up your environment](https://readie.org/docs/contributing/setup).

## License

Readie is open source under the
[Apache License 2.0](https://github.com/readie-project/readie/blob/main/LICENSE).
