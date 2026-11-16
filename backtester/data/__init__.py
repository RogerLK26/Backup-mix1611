from .base import Exchange, SymbolInfo
from .binance import Binance
from .bitget import Bitget
from .bybit import Bybit
from .gate import Gate
from .mexc import MEXC
from .okx import OKX

EXCHANGES = {
    "binance": Binance,
    "bybit": Bybit,
    "okx": OKX,
    "bitget": Bitget,
    "gate": Gate,
    "mexc": MEXC,
}


def get_exchange(name: str) -> Exchange:
    key = name.strip().lower()
    if key not in EXCHANGES:
        raise ValueError(f"Sàn không hỗ trợ: {name}. Hỗ trợ: {', '.join(EXCHANGES)}")
    return EXCHANGES[key]()
