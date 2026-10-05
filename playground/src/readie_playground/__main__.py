"""Run the service with ``python -m readie_playground``."""

import uvicorn

from readie_playground.app import create_app
from readie_playground.config import PlaygroundSettings


def main() -> None:
    """Start the ASGI server on the configured host and port."""
    settings = PlaygroundSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
