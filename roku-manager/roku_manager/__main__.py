import argparse
import os

from .engine import Engine
from .store import Store
from .web import make_server


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="Monitor and keep awake the Roku TVs on your network.")
    parser.add_argument("--host", default="0.0.0.0", help="address to serve the dashboard on (default: all)")
    parser.add_argument("--port", type=int, default=8765, help="dashboard port (default: 8765)")
    parser.add_argument("--data", default=os.path.join(here, "data", "config.json"), help="settings file")
    args = parser.parse_args()

    store = Store(args.data)
    engine = Engine(store)
    engine.start()
    server = make_server(args.host, args.port, store, engine)
    engine.add_log("info", f"Roku TV Manager running on http://{args.host}:{args.port} (settings: {args.data})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        server.server_close()


if __name__ == "__main__":
    main()
