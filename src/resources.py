"""What this machine actually gives us, and how many workers fit in it.

blurd targets cheap VMs and containers, where the interesting limit is memory,
not cores. The defaults used to be derived from `os.cpu_count()` alone, which
is wrong twice over: it ignores memory entirely, and inside a container it
reports the host's cores rather than the cgroup's share. A 512 MiB pod on a
32-core host would start 31 workers and be OOM-killed mid-job.

Measured on this pipeline (see spec/resources.md):

    resident memory  ~=  BASE  +  PER_WORKER x workers

Sizing assumes the costlier of the measured image sizes, because the cost of
guessing low is an OOM kill and the cost of guessing high is an idle core.
"""

import os
from pathlib import Path
from typing import Optional

# Fitted to measured points at 4000 px -- the costly case, so the one to plan
# with -- with the default configuration (trim between jobs on, onnxruntime
# arena off):
#
#   1 worker -> 192 MB   4 workers -> 416 MB   7 workers -> 672 MB
#   peak ~= 110 + 80 x workers       (predicts 670 MB at 7; measured 672)
#
# Rounded up, because guessing low costs an OOM kill while guessing high costs
# an idle core. Re-measure with `bash bench/memory.sh` if the pipeline changes.
#
# These constants assume `ort_arena` is off. Turning the arena back on adds
# roughly 40 MB per worker (measured 961 MB at 7 workers against 672), so a
# deployment that sets it must also set `workers` explicitly rather than
# trusting `auto` -- see spec/resources.md.
BASE_MB = 120
PER_WORKER_MB = 90

# Never fill the budget completely: the OS, the page cache and a burst of HTTP
# bodies all need somewhere to live.
HEADROOM = 0.85

# Queued uploads are held in memory -- see spec/jobs-and-codes.md: blurd never
# spools source bytes to disk, because it promises not to store originals. So
# the queue is a memory consumer, and it used to be bounded by JOB COUNT alone:
# 1000 jobs x ~1.5 MB is ~1.5 GB that the model below did not know about, on a
# box sized to 750 MB.
#
# Of whatever is left after the workers are paid for, this share may be spent
# holding queued payloads.
QUEUE_SHARE = 0.5
# A small floor, reserved before workers are sized. It does NOT have to clear
# MAX_BODY (64 MB, src/server.py): the queue admits any single upload when
# nothing is queued, so a large one can never be permanently rejected. Sizing a
# 512 MB box to reserve 64 MB would have cost it a whole worker to guard
# against a case the admission rule already handles.
QUEUE_MIN_MB = 16
# Past this there is no point: the queue is deep enough that the producer
# should be feeling backpressure instead.
QUEUE_MAX_MB = 512
# Used when the memory budget is unknown. ~170 images at the measured 1.5 MB.
QUEUE_FALLBACK_MB = 256

CGROUP_V2_MEM = Path("/sys/fs/cgroup/memory.max")
CGROUP_V1_MEM = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_V2_CPU = Path("/sys/fs/cgroup/cpu.max")


def available_cpus() -> float:
    """Cores this process may actually use.

    A cgroup quota is what a container is really allowed, and it is routinely
    fractional -- `cpu.max = 50000 100000` is half a core.
    """
    try:
        raw = CGROUP_V2_CPU.read_text().split()
        if raw and raw[0] != "max":
            quota, period = int(raw[0]), int(raw[1])
            if quota > 0 and period > 0:
                return max(0.1, quota / period)
    except (OSError, ValueError, IndexError):
        pass
    try:
        return float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return float(os.cpu_count() or 1)


def available_memory_mb() -> Optional[int]:
    """Memory this process may actually use, in MB.

    A cgroup limit beats the host's total: inside a container the host figure
    is a fiction, and acting on it is what gets a pod OOM-killed.
    """
    for path in (CGROUP_V2_MEM, CGROUP_V1_MEM):
        try:
            raw = path.read_text().strip()
            if raw and raw != "max":
                value = int(raw)
                # cgroup v1 reports a sentinel near 2^63 when unlimited.
                if 0 < value < (1 << 62):
                    return value // (1024 * 1024)
        except (OSError, ValueError):
            continue
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            # MemAvailable, not MemTotal: what is actually obtainable without
            # pushing the machine into swap.
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def worker_budget(memory_mb: Optional[int] = None, cpus: float = None) -> dict:
    """How many workers fit, and why.

    Returns the limit from each side so the answer can be explained rather than
    merely asserted -- "2 workers" is unhelpful, "2, because 1024 MB of memory
    allows 2 while 4 cores would allow 3" is not.
    """
    memory_mb = memory_mb if memory_mb is not None else available_memory_mb()
    cpus = cpus if cpus is not None else available_cpus()

    by_cpu = max(1, int(cpus) - 1) if cpus >= 2 else 1
    if memory_mb is None:
        return {"workers": by_cpu, "by_cpu": by_cpu, "by_memory": None,
                "memory_mb": None, "cpus": cpus, "limited_by": "cpu",
                "estimated_peak_mb": BASE_MB + PER_WORKER_MB * by_cpu}

    # The queue floor comes off the top. Workers are sized against what is left
    # once the queue's reservation is paid, so the two bounds cannot together
    # promise more memory than exists -- which is exactly what they did while
    # the queue was bounded by job count and the model ignored it entirely.
    usable = memory_mb * HEADROOM - QUEUE_MIN_MB
    by_memory = max(1, int((usable - BASE_MB) // PER_WORKER_MB))
    workers = max(1, min(by_cpu, by_memory))
    return {
        "workers": workers,
        "by_cpu": by_cpu,
        "by_memory": by_memory,
        "memory_mb": memory_mb,
        "cpus": cpus,
        "limited_by": "memory" if by_memory < by_cpu else "cpu",
        "estimated_peak_mb": BASE_MB + PER_WORKER_MB * workers,
    }


def estimate_peak_mb(workers: int, queue_mb: int = 0) -> int:
    """Peak resident memory, including whatever the queue is allowed to hold.

    `queue_mb` defaults to 0 so existing callers keep their meaning: it is the
    WORKER footprint they are asking about. Sizing decisions should pass it.
    """
    return BASE_MB + PER_WORKER_MB * int(workers) + int(queue_mb)


def queue_budget_mb(workers: int, memory_mb: Optional[int] = None) -> int:
    """How many megabytes of queued uploads we can afford to hold.

    Derived, not guessed: what is left of the budget once the workers are paid
    for, times QUEUE_SHARE. A deployment that wants a deeper queue should buy
    the memory or run fewer workers -- those are the real choices, and making
    the knob derive from them says so.
    """
    memory_mb = memory_mb if memory_mb is not None else available_memory_mb()
    if memory_mb is None:
        return QUEUE_FALLBACK_MB
    spare = memory_mb * HEADROOM - estimate_peak_mb(workers)
    return int(max(QUEUE_MIN_MB, min(QUEUE_MAX_MB, spare * QUEUE_SHARE)))


def effective_queue_bytes(cfg, workers: int) -> int:
    """The configured byte bound. `auto` means "what fits"."""
    configured = cfg.get("queue_max_bytes", "auto")
    if configured in (None, "", "auto"):
        return queue_budget_mb(workers) * 1024 * 1024
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        return queue_budget_mb(workers) * 1024 * 1024


def fits(workers: int, memory_mb: Optional[int] = None) -> Optional[bool]:
    """None when the budget is unknown -- an unknown is not a failure."""
    memory_mb = memory_mb if memory_mb is not None else available_memory_mb()
    if memory_mb is None:
        return None
    return estimate_peak_mb(workers) <= memory_mb * HEADROOM


def effective_workers(cfg) -> int:
    """The worker count actually used. `auto` means "as many as fit"."""
    configured = cfg.get("workers", "auto")
    if configured in (None, "", "auto"):
        return worker_budget()["workers"]
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        return worker_budget()["workers"]


def effective_http_threads(cfg, workers: int) -> int:
    """Derived from workers unless set.

    Each HTTP thread can hold a whole request body in memory, so this is a
    memory knob too, not only a concurrency one. Four per worker keeps the
    queue fed without letting a burst of uploads dwarf the workers themselves.
    """
    configured = cfg.get("http_threads", "auto")
    if configured in (None, "", "auto"):
        return max(4, min(32, workers * 4))
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        return max(4, min(32, workers * 4))


_TRIM = {"fn": None, "tried": False}


def malloc_trim() -> bool:
    """Hand freed memory back to the OS.

    glibc holds freed blocks in per-thread arenas rather than returning them,
    so a process that decodes a 10 MP image and frees it keeps the resident
    size anyway. Between jobs there is nothing worth holding.

    This is deliberately NOT the same as setting MALLOC_TRIM_THRESHOLD_ low:
    that was measured at -37% memory but -18% throughput, because it trims on
    every free, including inside the hot loop. Trimming once per job gets the
    memory back at a moment when nothing is in flight.

    Returns False where it does not apply (musl, macOS) -- not an error.
    """
    if not _TRIM["tried"]:
        _TRIM["tried"] = True
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.malloc_trim.argtypes = [ctypes.c_size_t]
            libc.malloc_trim.restype = ctypes.c_int
            _TRIM["fn"] = libc.malloc_trim
        except Exception:
            _TRIM["fn"] = None
    fn = _TRIM["fn"]
    if fn is None:
        return False
    try:
        fn(0)
        return True
    except Exception:
        return False


def describe() -> dict:
    b = worker_budget()
    queue_mb = queue_budget_mb(b["workers"], b["memory_mb"])
    return {
        "queue_budget_mb": queue_mb,
        "estimated_peak_with_queue_mb": estimate_peak_mb(b["workers"], queue_mb),
        "cpus": b["cpus"],
        "memory_mb": b["memory_mb"],
        "memory_source": ("cgroup" if CGROUP_V2_MEM.exists() or CGROUP_V1_MEM.exists()
                          else "/proc/meminfo"),
        "auto_workers": b["workers"],
        "limited_by": b["limited_by"],
        "estimated_peak_mb": b["estimated_peak_mb"],
        "model": f"{BASE_MB} MB base + {PER_WORKER_MB} MB per worker",
    }
