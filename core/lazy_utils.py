"""
Lazy loading utilities for expensive components.
Improves startup time by deferring non-critical initialization.
"""
from __future__ import annotations

from functools import cached_property
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class LazyProperty(Generic[T]):
    """Descriptor for lazy property initialization."""

    def __init__(self, factory: Callable[[], T]):
        self._factory = factory
        self._value: T | None = None
        self._initialized = False

    def __get__(self, obj: Any, objtype: Any | None = None) -> T:
        if obj is None:
            return self._factory()  # type: ignore
        if not self._initialized:
            self._value = self._factory()
            self._initialized = True
        return self._value  # type: ignore

    def __set__(self, obj: Any, value: T) -> None:
        self._value = value
        self._initialized = True

    def reset(self) -> None:
        self._value = None
        self._initialized = False


def lazy_property(factory: Callable[[], T]) -> property:
    """Create a lazily initialized property."""
    return property(LazyProperty(factory).__get__)  # type: ignore


class LazyGetter(Generic[T]):
    """Lazy getter with memoization."""

    def __init__(self, factory: Callable[[], T], name: str = ""):
        self._factory = factory
        self._name = name
        self._value: T | None = None
        self._loaded = False

    def get(self) -> T:
        if not self._loaded:
            self._value = self._factory()
            self._loaded = True
        return self._value

    @property
    def ready(self) -> bool:
        return self._loaded

    def reset(self) -> None:
        self._value = None
        self._loaded = False


class LazyLoader:
    """Container for managing multiple lazy components."""

    def __init__(self):
        self._loaders: dict[str, LazyGetter] = {}

    def register(self, name: str, factory: Callable[[], Any]) -> None:
        self._loaders[name] = LazyGetter(factory, name)

    def get(self, name: str) -> Any:
        loader = self._loaders.get(name)
        if loader is None:
            raise KeyError(f"Lazy loader '{name}' not registered")
        return loader.get()

    def is_ready(self, name: str) -> bool:
        loader = self._loaders.get(name)
        return loader.ready if loader else False

    def reset(self, name: str | None = None) -> None:
        if name is None:
            for loader in self._loaders.values():
                loader.reset()
        else:
            loader = self._loaders.get(name)
            if loader:
                loader.reset()


_global_loader = LazyLoader()


def register_lazy(name: str, factory: Callable[[], Any]) -> None:
    _global_loader.register(name, factory)


def get_lazy(name: str) -> Any:
    return _global_loader.get(name)


def lazy_ready(name: str) -> bool:
    return _global_loader.is_ready(name)