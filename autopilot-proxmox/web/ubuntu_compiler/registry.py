"""Step-type → compile function registry for Ubuntu."""
from __future__ import annotations

from typing import Any, Callable

from .types import StepOutput, UbuntuCompileError

# A compile function takes (params: dict, credentials: dict) and returns a StepOutput.
# `credentials` is a dict {id: decrypted_payload_dict} so steps can look up refs.
CompileFn = Callable[[dict[str, Any], dict[int, dict[str, Any]]], StepOutput]

_REGISTRY: dict[str, CompileFn] = {}


def register(step_type: str) -> Callable[[CompileFn], CompileFn]:
    def decorator(fn: CompileFn) -> CompileFn:
        if step_type in _REGISTRY:
            raise RuntimeError(f"step_type {step_type!r} already registered")
        _REGISTRY[step_type] = fn
        return fn
    return decorator


def compile_step(
    step_type: str,
    params: dict[str, Any],
    credentials: dict[int, dict[str, Any]],
) -> StepOutput:
    try:
        fn = _REGISTRY[step_type]
    except KeyError as e:
        raise UbuntuCompileError(f"unknown Ubuntu step_type: {step_type}") from e
    return fn(params, credentials)


def is_ubuntu_step(step_type: str) -> bool:
    return step_type in _REGISTRY


def registered_step_types() -> list[str]:
    return sorted(_REGISTRY.keys())


def _load_all_steps() -> None:
    """Eagerly import step modules so their @register decorators run.
    Called from the package __init__. Empty for now — each subsequent task
    adds one import line as it lands.
    """
    from .steps import install_ubuntu_core  # noqa: F401
    from .steps import create_ubuntu_user  # noqa: F401
