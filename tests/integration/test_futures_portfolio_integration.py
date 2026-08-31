"""Integration tests for futures support in portfolio manager."""

import pytest
from src.portfolio.portfolio_manager_v2 import PortfolioManagerV2
from src.execution.sizing.contract_sizer import FuturesContractSizer
from src.execution.contracts.registry import get_registry


class TestFuturesPortfolioIntegration:
    """Test futures mode integration with PortfolioManagerV2."""
    
    # TODO: Test portfolio manager initialization with futures_mode=True
    
    # TODO: Test position sizing using FuturesContractSizer
    
    # TODO: Test margin calculation and tracking
    
    # TODO: Test backward compatibility (spot mode still works)
    
    # TODO: Test end-to-end backtest with futures contracts
    
    pass
