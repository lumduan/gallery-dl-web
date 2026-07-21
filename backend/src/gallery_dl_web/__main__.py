"""Entrypoint: ``python -m gallery_dl_web`` -> uvicorn serving the FastAPI app."""

import uvicorn

from gallery_dl_web.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "gallery_dl_web.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
