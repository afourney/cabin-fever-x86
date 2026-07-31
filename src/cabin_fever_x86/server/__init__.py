"""The game server: the companion, its tools, and the machine it plays on."""

from cabin_fever_x86.server._ai_client import create_client
from cabin_fever_x86.server._game import Game

__all__ = ["Game", "create_client"]
