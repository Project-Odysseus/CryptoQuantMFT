"""Data models for futures contracts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class FuturesContract:
    """Represents a standardized futures contract specification.
    
    Attributes:
        symbol: Contract symbol (e.g., "ES", "BTC", "CL", "ENREUR")
        exchange: Exchange name (e.g., "CME", "Kraken", "CBOT")
        contract_size: Notional value per 1 contract unit
        multiplier: Price tick to notional conversion factor
        tick_size: Minimum price increment
        margin_initial: Initial margin requirement (% or notional)
        margin_maintenance: Maintenance margin requirement (%)
        active_months: Contract expiration months (e.g., "ZHJMU")
        description: Human-readable contract description
        last_updated: Timestamp of last spec update
    """
    
    symbol: str
    exchange: str
    contract_size: float
    multiplier: float
    tick_size: float
    margin_initial: float
    margin_maintenance: float
    active_months: str
    description: str
    last_updated: datetime
    
    # Optional fields
    currency: Optional[str] = None
    underlying_asset: Optional[str] = None
    contract_unit: Optional[str] = None  # e.g., "USD", "barrels", "troy oz"
    
    def __post_init__(self):
        """Validate contract specification."""
        # TODO: Add validation logic
        pass
    
    def __repr__(self) -> str:
        """Return string representation."""
        # TODO: Implement
        pass
