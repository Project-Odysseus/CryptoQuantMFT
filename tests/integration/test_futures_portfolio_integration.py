"""Integration tests for futures support in portfolio manager."""

import pytest

pytestmark = pytest.mark.skip(
    reason="Futures portfolio integration is a placeholder and still depends on the deferred futures track."
)


class TestFuturesPortfolioIntegration:
    """Placeholder tests for a future futures-portfolio integration lane."""
    
    # TODO: Test portfolio manager initialization with futures_mode=True
    
    # TODO: Test position sizing using FuturesContractSizer
    
    # TODO: Test margin calculation and tracking
    
    # TODO: Test backward compatibility (spot mode still works)
    
    # TODO: Test end-to-end backtest with futures contracts
    
    pass
