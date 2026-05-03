import functools


def cached(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


@cached
def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)
