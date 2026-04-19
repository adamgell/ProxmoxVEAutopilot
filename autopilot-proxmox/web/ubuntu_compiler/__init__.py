"""Ubuntu sequence compiler: sequence → autoinstall.yaml + per-clone cloud-init."""
from .registry import _load_all_steps, compile_step, is_ubuntu_step, registered_step_types
from .types import StepOutput, UbuntuCompileError

_load_all_steps()

# Import after registry is populated so all step types are known.
from .assembler import compile_sequence  # noqa: E402

__all__ = [
    "StepOutput",
    "UbuntuCompileError",
    "compile_sequence",
    "compile_step",
    "is_ubuntu_step",
    "registered_step_types",
]
