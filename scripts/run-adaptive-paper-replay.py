#!/usr/bin/env python3
"""Run the isolated capital-adaptive replay and publish its comparison artifact."""

from qsim.services.adaptive_replay import run_adaptive_replay


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20260801")
    parser.add_argument("--end")
    args = parser.parse_args()
    result = run_adaptive_replay(start=args.start, end=args.end)
    print(json.dumps({
        "artifact": result["artifact"], "start": result["start"], "end": result["end"],
        "source_market_as_of": result["source_market_as_of"], "trade_count": result["trade_count"],
    }, ensure_ascii=False))
