"""Entry point for running one call in a freshly spawned interpreter.

Invoked as ``python -m readie_executor._invoke_subprocess <result-path>``:
reads the call payload from stdin, invokes it, and writes the pickled result
envelope to ``<result-path>``.

This exists for the call that just installed packages (see ``install.py``): a
package installed for this call can be a native extension at a different
version than whatever a checkpoint restore left resident in the long-lived
executor process (``preimport.py``). Running the call here instead means
nothing was ever preloaded, so it always reads what is on disk right now,
rather than risking two ABI-incompatible copies of the same extension in one
address space -- which reimporting in the already-warm process can crash on
outright rather than raise.
"""

from __future__ import annotations

import sys
import traceback

from readie_executor import protocol
from readie_executor.codec import decode_call, encode_result


def main() -> None:
    """Decode the call from stdin, invoke it, and write the result envelope."""
    result_path = sys.argv[1]
    raw = sys.stdin.buffer.read()

    try:
        call = decode_call(raw)
        value = call.invoke()
    except BaseException as exc:  # noqa: BLE001 - any failure becomes a response, not a crash
        envelope = protocol.failure_envelope(exc, traceback.format_exc())
    else:
        envelope = protocol.success_envelope(value)

    with open(result_path, "wb") as handle:  # noqa: PTH123 - argv path, not a project asset
        handle.write(encode_result(envelope))


if __name__ == "__main__":
    main()
