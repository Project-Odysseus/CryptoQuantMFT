"""In-memory caching of expensive features, keyed by the data's content and the parameters (plan 2.8).

    @cached
    def regime_scores(bars, config): ...

The key is the fingerprint of every DataFrame or Series argument plus the config hash of every other argument, so a
cached value is reused only for identical inputs, and a changed parameter or a revised data snapshot recomputes.
Re-running a notebook cell after changing one overlay setting then recomputes only what that setting touches.
"""

from __future__ import annotations

import functools
import hashlib
from collections import OrderedDict
from typing import Any, Callable, TypeVar

import pandas as pd

from quantlab.config import config_hash

F = TypeVar("F", bound=Callable[..., Any])
MAX_ENTRIES = 256


def _key_part(value: Any) -> str:
    if isinstance(value, (pd.DataFrame, pd.Series)):
        return hashlib.sha256(pd.util.hash_pandas_object(value, index=True).values.tobytes()).hexdigest()[:16]
    try:
        return config_hash(value)
    except TypeError:
        return repr(value)


def cached(function: F) -> F:
    """Memoise `function` on the content of its arguments (an LRU of `MAX_ENTRIES` results; copies are returned)."""
    store: OrderedDict[str, Any] = OrderedDict()

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = "|".join([_key_part(arg) for arg in args] + [f"{name}={_key_part(value)}" for name, value in sorted(kwargs.items())])
        if key in store:
            store.move_to_end(key)
            value = store[key]
        else:
            value = function(*args, **kwargs)
            store[key] = value
            if len(store) > MAX_ENTRIES:
                store.popitem(last=False)
        return value.copy() if isinstance(value, (pd.DataFrame, pd.Series)) else value

    wrapper.cache_clear = store.clear  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]
