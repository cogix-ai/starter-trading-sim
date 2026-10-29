"""Your Trading Sim agent. THIS IS THE ONLY FILE YOU EDIT.

The market streams one bar at a time. Your opponent sees the same market.
Highest final equity wins. Test offline with ./test, then enter with ./submit.
"""


def trade(task, tools):
    """Called once per match. Loop over the bars and place orders.

    Each obs has:
      obs["bar_index"], obs["horizon"]     where you are, and how many bars in total
      obs["cash"], obs["equity"]           your money
      obs["positions"]                     {"SYN1": qty}
      obs["bars"]["SYN1"]                  every bar so far: open, high, low, close, volume
    Orders go out at the end of the bar:
      tools.market.place(instrument="SYN1", side="buy", type="market", qty=1)
    """
    for obs in tools.market:
        # TODO: your strategy. This one buys when price is above its 10-bar average
        # and sells when it falls below.
        closes = [b["close"] for b in obs["bars"]["SYN1"]]
        held = obs["positions"].get("SYN1", 0)
        if len(closes) < 10:
            continue
        average = sum(closes[-10:]) / 10
        if closes[-1] > average and held == 0:
            tools.market.place(instrument="SYN1", side="buy", type="market", qty=1)
        elif closes[-1] < average and held > 0:
            tools.market.place(instrument="SYN1", side="sell", type="market", qty=held)
