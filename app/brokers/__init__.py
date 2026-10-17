from .base import Broker, BrokerError
from .paper import PaperBroker
from .gate import GateBroker, test_credentials

__all__ = ["Broker", "BrokerError", "PaperBroker", "GateBroker", "test_credentials", "make_broker"]


def make_broker(settings) -> Broker:
    """Pick the broker according to the settings."""
    if settings.trade_mode == "live" and settings.data_source == "gate":
        return GateBroker(settings)
    return PaperBroker(settings)
