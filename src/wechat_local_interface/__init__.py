"""Read-only interfaces for decrypted local WeChat data."""

from .protocol import PROTOCOL_VERSION, WeChatProtocol
from .source import SCHEMA_VERSION, WeChatSource

__all__ = ["PROTOCOL_VERSION", "SCHEMA_VERSION", "WeChatProtocol", "WeChatSource"]
