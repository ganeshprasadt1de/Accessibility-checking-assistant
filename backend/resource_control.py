from __future__ import annotations

import os
import sys
import time


LOW_END_ENV = "ACC_LOW_END_MODE"


def low_end_enabled() -> bool:
    return os.environ.get(LOW_END_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def low_end_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment that limits CPU and Java pressure without changing checks."""
    env = dict(base or os.environ)
    env[LOW_END_ENV] = "1"
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        env.setdefault(key, "1")
    java_options = env.get("JAVA_TOOL_OPTIONS", "").strip()
    additions = "-XX:ActiveProcessorCount=2 -Xmx2g"
    if additions not in java_options:
        env["JAVA_TOOL_OPTIONS"] = f"{java_options} {additions}".strip() if java_options else additions
    return env


def configure_current_process_low_end() -> None:
    """Lower this process priority where the operating system permits it."""
    if not low_end_enabled():
        return
    if sys.platform.startswith("win"):
        try:
            import ctypes

            below_normal_priority_class = 0x00004000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            if not kernel32.SetPriorityClass(handle, below_normal_priority_class):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            return
    else:
        try:
            os.nice(5)
        except OSError:
            return


def low_end_throttle(index: int, *, interval: int = 16, delay_s: float = 0.002) -> None:
    if low_end_enabled() and interval > 0 and index % interval == 0:
        time.sleep(delay_s)
