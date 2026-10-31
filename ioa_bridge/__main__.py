"""Launch the bridge on loopback only: python -m ioa_bridge."""
import uvicorn

from .server import Settings, create_app


if __name__ == "__main__":
    uvicorn.run(create_app(settings=Settings.from_env()),
                host="127.0.0.1", port=8791, access_log=False)
