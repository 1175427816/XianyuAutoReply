#!/usr/bin/env python3
import argparse

import web_admin


def main():
    parser = argparse.ArgumentParser(description="Start Xianyu Auto Reply monitor and Web dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    result = web_admin.start_monitor_process()
    print(f"[start_all] {result.get('message')}")
    print(f"[start_all] Web dashboard: http://{args.host}:{args.port}")

    web_admin.run_server(args.host, args.port)


if __name__ == "__main__":
    main()
