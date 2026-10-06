"""A per-request memo for read-only page loads.

The month-end screens ask the same questions over and over inside one request (the settings row, a month's level, a property's
closes, the same lines for every earlier month of a reconciliation): dozens to hundreds of identical queries. Inside a memo scope
each is answered once.

The scope is only ever opened for a GET or HEAD request (see core.middleware.RequestMemoMiddleware), so nothing that changes data is
running while it is active, and it is closed when the request ends. Outside a scope - a management command, the scheduler, a test calling
the ledger directly, a POST - every call goes straight to the database exactly as before, so there is nothing stale to reason about.
The one place that writes during a page load (a reservation given its unit) calls forget() so what was read before it is read again."""
import threading

_local = threading.local()


def begin():
    _local.store = {}


def end():
    _local.store = None


def active():
    return getattr(_local, 'store', None) is not None


def get(key, compute):
    """The value remembered under `key` for this request, computed (and remembered) the first time. With no scope open it is
    just compute()."""
    store = getattr(_local, 'store', None)
    if store is None:
        return compute()
    try:
        return store[key]
    except KeyError:
        value = store[key] = compute()
        return value


def lookup(key):
    """What is remembered under `key`, or None (also None with no scope open)."""
    store = getattr(_local, 'store', None)
    return None if store is None else store.get(key)


def put(key, value):
    store = getattr(_local, 'store', None)
    if store is not None:
        store[key] = value


def forget(*prefix):
    """Drop everything remembered whose key starts with `prefix` (no prefix drops it all)."""
    store = getattr(_local, 'store', None)
    if store is None:
        return
    for key in [k for k in store if k[:len(prefix)] == prefix]:
        del store[key]
