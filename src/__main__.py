"""Serve the KYC investigation Gradio UI."""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from kyc_agent.app import launch_app
from kyc_agent.llm import model_summary
from kyc_agent.runtime import configure_logging


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="KYC public-domain investigation UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true", help="Create a public Gradio link")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Console log level for investigation events (default: INFO)",
    )
    args = parser.parse_args(argv)

    configure_logging(args.log_level)
    print(f"KYC UI → http://{args.host}:{args.port}")
    print(f"Models: {model_summary()}")
    print("Investigation logs stream to stderr (use --log-level DEBUG for more detail)")
    launch_app(host=args.host, port=args.port, share=args.share)


if __name__ == "__main__":
    main()
