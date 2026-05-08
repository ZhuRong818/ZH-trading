"""
EntryLock — one position at a time with cooldown.
"""

import time


class EntryLock:

    def __init__(self, cooldown_seconds: float = 10.0):
        self.cooldown = cooldown_seconds
        self.has_position = False
        self._last_trade_time = 0.0

    def can_enter(self) -> bool:
        if self.has_position:
            return False
        if time.time() - self._last_trade_time < self.cooldown:
            return False
        return True

    def on_signal(self):
        """Lock immediately when emitting a signal."""
        self.has_position = True
        self._last_trade_time = time.time()

    def on_fill_buy(self):
        self.has_position = True

    def on_fill_sell(self):
        self.has_position = False

    def on_cancel(self):
        self.has_position = False
