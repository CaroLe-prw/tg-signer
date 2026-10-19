from importlib import import_module

__all__ = ["build_ui", "main", "AUTH_CODE_ENV"]


def __getattr__(name):
    # Workers and lifecycle tests do not need the optional NiceGUI dependency.
    if name in __all__:
        return getattr(import_module("tg_signer.webui.app"), name)
    raise AttributeError(name)
