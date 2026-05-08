"""
EdgeSkill — compares fair value vs market price, picks the better side.
"""

from skills.types import FairValue, RollingMarket, Edge


class EdgeSkill:
    """Compares UP and DOWN edges, returns the best tradeable side."""

    def best_edge(self, fair: FairValue, market: RollingMarket) -> Edge:
        edge_up = fair.fair_up - market.up_price if market.up_price > 0 else -1
        edge_down = fair.fair_down - market.down_price if market.down_price > 0 else -1

        if edge_up >= edge_down:
            return Edge(
                direction="UP",
                fair=fair.fair_up,
                market_price=market.up_price,
                edge=edge_up,
                token_id=market.up_token_id,
                reason=fair.reason,
            )

        return Edge(
            direction="DOWN",
            fair=fair.fair_down,
            market_price=market.down_price,
            edge=edge_down,
            token_id=market.down_token_id,
            reason=fair.reason,
        )
