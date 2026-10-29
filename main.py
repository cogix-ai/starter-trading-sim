"""DO NOT EDIT. Connects agent.py to the Cogix runtime. Edit agent.py instead."""

import sys

from cogix_sdk import Agent

import agent as yours

app = Agent(name="trading-sim-agent", version="1.0.0")
app.session("trading_sim")(yours.trade)


def selftest() -> None:
    """Run trade() over a small fake market with the same fields as the real one.

    The prices are made up and nothing is filled: this checks that your code
    runs and reads the data correctly, not that it makes money.
    """
    import io
    import json
    import math

    from cogix_sdk.session import (FRAME_OBSERVATION, FRAME_SESSION_END,
                                   FRAME_SESSION_START, run_session)

    horizon, bars, frames = 60, [], []
    for i in range(horizon):
        close = round(100 + 5 * math.sin(i / 6) + i * 0.1, 2)
        bars.append({"ts": i, "open": close, "high": close + 0.5, "low": close - 0.5,
                     "close": close, "volume": 1000})
        frames.append({"type": FRAME_OBSERVATION, "obs": {
            "bar_index": i, "horizon": horizon, "cash": 10000.0, "equity": 10000.0,
            "positions": {}, "bars": {"SYN1": list(bars)}, "open_orders": [],
            "session": {}}})
    frames.append({"type": FRAME_SESSION_END})
    start = {"type": FRAME_SESSION_START,
             "task": {"challenge_id": "trading_sim", "budgets": {"wall_seconds": 30}}}
    out = io.StringIO()
    stdin = io.StringIO("".join(json.dumps(f) + "\n" for f in frames))
    run_session({"trading_sim": yours.trade}, start, stdin, out)
    orders = sum(1 for line in out.getvalue().splitlines() if line.strip()
                 for act in json.loads(line).get("actions") or [] if act.get("kind") == "place")
    errors = [l for l in out.getvalue().splitlines() if '"error"' in l]
    if errors:
        raise SystemExit("trade() raised an error:\n" + errors[0][:500])
    print(f"trading: OK, ran {horizon} bars, sent {orders} order(s)")


if __name__ == "__main__":
    if sys.argv[1:] == ["selftest"]:
        selftest()
    else:
        sys.exit(app.run_batch())
