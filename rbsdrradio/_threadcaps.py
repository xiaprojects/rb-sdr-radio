"""Cap numeric-library thread pools to one thread. Import BEFORE numpy/scipy.

Reference: rb-vor field bug, 2026-07-25 -- a daemon pegged 100% on all four Pi cores
while doing perhaps 4% of real work.

Cause: the polyphase FIR (`wins @ taps` in dsp.py) and the scipy filters hand
off to the platform BLAS (OpenBLAS on Raspberry Pi OS). OpenBLAS defaults to one
worker thread per core and **busy-waits** between calls, so a single-threaded
1 Hz DSP shows up as every core at 100%. On a 4-core Pi that also steals cycles
from the SDR reader thread, which can starve the pipeline.

For this workload the matrices are tiny (a few thousand elements); threading them
is pure overhead. Pinning every backend to one thread drops CPU to a few percent
of one core with no change in output.

This must run before numpy is imported, because OpenBLAS reads these variables at
load time. `rbsdrradio/__main__.py` imports this module first, ahead of everything.
"""

from __future__ import annotations

import os

# Each of these governs a different backend; set all, since which one numpy was
# built against varies by image (OpenBLAS, ATLAS, MKL, or the reference BLAS).
_VARS = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
)


def apply(threads: int = 1) -> list[str]:
    """Set every thread-count variable that is not already set. Returns the ones
    this call changed, for logging. Never overrides an operator's explicit value."""
    changed = []
    for var in _VARS:
        if var not in os.environ:
            os.environ[var] = str(threads)
            changed.append(var)
    return changed


# Applied on import, so merely importing this module before numpy is enough.
_CHANGED = apply(1)
