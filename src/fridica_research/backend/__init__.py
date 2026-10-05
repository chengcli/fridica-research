"""Driver backends (R15): `fridica` (the control socket) and `bootstrap` (the feed journal with subprocess workers)."""
from __future__ import annotations

from ..config import Config
from .protocol import ControlError, DriverBackend, Unavailable

__all__ = ["ControlError", "DriverBackend", "Unavailable", "build_backend"]


def build_backend(cfg: Config, **kw) -> DriverBackend:
    """The backend `[backend] kind` names; `kw` (runner, clock, sleep) reaches the bootstrap backend for tests."""
    if cfg.backend.kind == "bootstrap":
        from .bootstrap import BootstrapBackend
        return BootstrapBackend(cfg, **kw)
    if cfg.backend.kind != "fridica": raise ValueError(f"[backend] unknown kind {cfg.backend.kind!r}")
    from ..client import Client, read_capability
    from .fridica import FridicaBackend
    token = read_capability(cfg.capability_file) if cfg.capability_file else None
    return FridicaBackend(Client(cfg.socket_path, token))
