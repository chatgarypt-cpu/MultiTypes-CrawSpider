import argparse
import json
from wrc.site import serve
from wrc.observe import observe
from wrc.collect import collect

def main():
    parser = argparse.ArgumentParser(description="Web Reverse Crawler Agent v0.1")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    obs = commands.add_parser("observe")
    obs.add_argument("--url", default="http://127.0.0.1:8765")
    col = commands.add_parser("collect")
    col.add_argument("--api", default="http://127.0.0.1:8765/api/articles")
    args = parser.parse_args()
    if args.command == "serve": serve()
    elif args.command == "observe": print(json.dumps(observe(args.url), ensure_ascii=False, indent=2))
    elif args.command == "collect": print(f"Saved {collect(args.api)} unique articles")

if __name__ == "__main__": main()
