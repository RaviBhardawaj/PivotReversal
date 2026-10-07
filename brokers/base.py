"""
Common broker interface used by the order engine.

Every broker adapter (Angel One, Dhan, XTS) implements the same small set of calls,
so the engine never needs to know which broker it is talking to.

Order types passed in are normalised:  "MARKET", "LIMIT", "SL" (stop-loss limit).
Sides are "BUY" / "SELL".  Product is "INTRADAY" or "CARRY".
"""

FILLED    = "FILLED"
PENDING   = "PENDING"      # open, trigger pending, in transit, partially filled
REJECTED  = "REJECTED"
CANCELLED = "CANCELLED"
UNKNOWN   = "UNKNOWN"


class BrokerError(Exception):
    """Raised for any broker failure. The message is safe to show and log (never holds keys)."""


class OrderStatus:
    __slots__ = ("order_id", "status", "avg_price", "filled_qty", "message", "tag")

    def __init__(self, order_id="", status=UNKNOWN, avg_price=0.0, filled_qty=0, message="", tag=""):
        self.order_id   = str(order_id or "")
        self.status     = status
        self.avg_price  = float(avg_price or 0)
        self.filled_qty = int(float(filled_qty or 0))
        self.message    = str(message or "")
        self.tag        = str(tag or "")

    def __repr__(self):
        return f"OrderStatus({self.order_id} {self.status} {self.filled_qty}@{self.avg_price} {self.message!r})"


def tick_round(px: float, tick: float = 0.05) -> float:
    """Round a price to the exchange tick (0.05 for index options)."""
    if px <= 0:
        return 0.0
    return round(round(px / tick) * tick, 2)


def to_float(v, default=0.0):
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return float(v)
    except Exception:
        return default


class BrokerAdapter:
    """Base class. Subclasses fill in every method marked NotImplementedError."""

    name = "BASE"
    # .env keys this broker needs (shown in the Settings window; secrets are masked)
    env_keys = []
    secret_keys = []

    def __init__(self, creds: dict, log=print):
        self.creds = creds or {}
        self.log = log
        self.connected = False
        self.account = ""

    # ── session ─────────────────────────────────────────────
    def login(self) -> str:
        """Connect and return a short account label. Raises BrokerError."""
        raise NotImplementedError

    def funds(self):
        """Available cash/margin in rupees, or None if the broker does not say."""
        return None

    # ── instruments ─────────────────────────────────────────
    def resolve(self, inst: dict) -> dict:
        """
        inst carries: symbol, exchange ('NFO'/'BFO'), token (exchange token), tsym (Angel symbol),
        expiry ('28OCT2026'), strike, opt_type, lot_size.
        Returns inst with 'inst_id' (the broker's own instrument ID) and 'segment' filled in.
        """
        raise NotImplementedError

    # ── orders ──────────────────────────────────────────────
    def place(self, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag="") -> str:
        """Send an order, return the broker order ID. Raises BrokerError."""
        raise NotImplementedError

    def modify(self, order_id, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        raise NotImplementedError

    def cancel(self, order_id, inst=None, tag=""):
        raise NotImplementedError

    def orders(self) -> dict:
        """Today's order book: {order_id: OrderStatus}."""
        raise NotImplementedError

    def positions(self) -> dict:
        """Open net positions: {inst_id (str): net qty (int)}."""
        raise NotImplementedError
