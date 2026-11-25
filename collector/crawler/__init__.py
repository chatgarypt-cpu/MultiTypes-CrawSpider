"""Modular crawler package: one module per discovery channel, shared core helpers.

Imports are lazy so light modules (config, store) work without the heavy deps.
"""

def __getattr__(name):
    if name == "collect_site":
        from crawler.runner import collect_site
        return collect_site
    if name == "CHANNELS":
        from crawler.runner import CHANNELS
        return CHANNELS
    raise AttributeError(name)
