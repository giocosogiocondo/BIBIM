"""Release graph-bound method caches at the end of an extraction session."""


def clear_method_caches(*objects):
    for obj in objects:
        if obj is None:
            continue
        for name in dir(type(obj)):
            method = getattr(type(obj), name, None)
            if hasattr(method, "cache_clear"):
                method.cache_clear()
