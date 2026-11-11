"""What the AM chain costs on this machine, for each detector and noise reduction setting.

    cd /opt/addons/rb-sdr-radio && python3 -m tools.cpu_cost

Synthetic blocks, so the daemon can keep the dongle. Times one block of 32768 samples,
32 ms of air, through the whole chain from IQ to audio, and reports the share of one core.
On a laptop with `schedutil`, `top` shows more than this at light load: the same work at
a lower clock.
"""

from __future__ import annotations

# isort: off
from rbsdrradio import _threadcaps  # noqa: F401  -- caps BLAS threads before numpy loads
# isort: on

import argparse
import time

import numpy as np

from rbsdrradio import synth
from rbsdrradio.dsp import FS_HZ, AmDemod

BLOCK = 32_768
VARIANTS = (("envelope", 0.0), ("envelope", 12.0), ("synchronous", 0.0), ("synchronous", 12.0))


def cost_ms(detector: str, noise_reduction_db: float, blocks: list[np.ndarray],
            count: int) -> float:
    """Best of three runs, so a busy moment elsewhere does not count."""
    best = float("inf")
    for _ in range(3):
        demod = AmDemod(detector=detector, noise_reduction_db=noise_reduction_db)
        for block in blocks[:4]:                        # filters and meter filled
            demod.process(block)
        start = time.perf_counter()
        for i in range(count):
            demod.process(blocks[i % len(blocks)])
        best = min(best, (time.perf_counter() - start) / count * 1000.0)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--blocks", type=int, default=300, help="blocks per run (default 300)")
    args = ap.parse_args()
    seconds = 16 * BLOCK / FS_HZ
    iq = (synth.am(seconds, -30.0) + synth.noise(seconds, -40.0)).astype(np.complex64)
    blocks = [iq[i:i + BLOCK] for i in range(0, iq.size - BLOCK + 1, BLOCK)]
    block_ms = BLOCK / FS_HZ * 1000.0
    for detector, nr_db in VARIANTS:
        ms = cost_ms(detector, nr_db, blocks, args.blocks)
        nr = f"noise reduction {nr_db:g} dB" if nr_db else "no noise reduction"
        print(f"  {detector:11s}  {nr:24s} {ms:6.2f} ms per {block_ms:.0f} ms block, "
              f"{100.0 * ms / block_ms:4.1f} % of one core")


if __name__ == "__main__":
    main()
