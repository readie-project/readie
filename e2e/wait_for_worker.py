"""Wait for the router to place an execution before running the assertions."""

import os
import time

import readie


def main():
    def probe():
        return "ready"

    deadline = time.monotonic() + 30
    settings = readie.Settings(
        router_uri=os.environ["READIE_E2E_ENDPOINT"], timeout=45, stream_logs=False
    )
    with readie.Client(settings) as client:
        check = readie.remote(client=client)(probe)
        while True:
            try:
                if check() != "ready":
                    raise RuntimeError("unexpected result from the readiness probe")
                return
            except readie.ClusterUnavailableError as error:
                if (
                    "no workers are available" not in str(error)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(1)


if __name__ == "__main__":
    main()
