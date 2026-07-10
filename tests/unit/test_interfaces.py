import pytest

from clav.interfaces.analyst import Analyst
from clav.interfaces.broker import Broker
from clav.interfaces.market_data import MarketDataSource
from clav.interfaces.news import NewsSource


@pytest.mark.parametrize("iface", [MarketDataSource, Broker, NewsSource, Analyst])
def test_interfaces_cannot_be_instantiated_directly(iface: type) -> None:
    with pytest.raises(TypeError):
        iface()  # abstract methods unimplemented
