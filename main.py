from __future__ import annotations

import argparse
import multiprocessing


def main() -> None:
    # In a frozen Windows build this must run before importing the backend.
    # Otherwise every ProcessPool worker imports the complete HTTP backend
    # first and creates a misleading backend_*.log before freeze_support()
    # redirects it to the worker entry point.
    multiprocessing.freeze_support()
    from server import DEFAULT_PORT, HOST, VERSION, serve

    parser = argparse.ArgumentParser(description=f"Dragon Nest ACT V{VERSION} engine")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
