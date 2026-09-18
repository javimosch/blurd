# Resource footprint and sizing

blurd targets cheap VMs. The binding constraint there is memory, not cores, and
until 0.12.0 the defaults ignored it entirely.

## The defect this fixes

`workers` defaulted to `os.cpu_count() - 1`. That is wrong twice over: it takes
no account of memory, and inside a container it reports the **host's** cores
rather than the cgroup's share. A 512 MiB pod on a 32-core host would start 31
workers.

Measured **at 0.12.0**, 30 × 10.7 MP images at 8 concurrent, in a 512 MB
container. The figures are kept as recorded rather than restated: `auto` chose
2 workers against the constants of the day, and chooses 3 against the current
ones (the arena came off in 0.13.0, and the queue reservation arrived in
0.15.0). The conclusion is what carries forward, not the arithmetic.

| configuration | outcome |
|---|---|
| `BLURD_WORKERS=7` (the old default on an 8-core host) | **OOM-killed**, exit 137 |
| `BLURD_WORKERS=auto` (2 workers at the time) | **30/30 completed**, peak 503 MB |

## The model

```
peak resident  ~=  BASE_MB  +  PER_WORKER_MB x workers
```

Fitted to measurements at 4000 px — the costly case, so the one to plan with —
with the default configuration (trim between jobs on, onnxruntime arena off):

| workers | measured | model |
|---:|---:|---:|
| 1 | 192 MB | 210 MB |
| 4 | 416 MB | 480 MB |
| 7 | 672 MB | 750 MB |

The constants (`120 + 90 x workers`) sit deliberately above the fit: guessing
low costs an OOM kill and guessing high costs an idle core.

**These constants assume `ort_arena` is off**, which is the default since
0.13.0. Turning the arena back on adds roughly 40 MB per worker, and `auto`
sizing will then be wrong — see below.

Re-measure with `bash bench/memory.sh` whenever the pipeline changes what it
holds in memory, and update the constants in `src/resources.py`.

## Auto sizing

`workers: auto` (the default) takes the lower of what CPU and memory allow, and
is **cgroup-aware**: `memory.max` and `cpu.max` beat the host's figures, because
inside a container the host figures are a fiction.

| VM | workers | est. peak | limited by |
|---|---:|---:|---|
| 512 MB / 1 vCPU | 1 | 210 MB | cpu |
| 1 GB / 2 vCPU | 1 | 210 MB | cpu |
| 2 GB / 4 vCPU | 3 | 390 MB | cpu |
| 1 GB / 8 vCPU | 7 | 750 MB | cpu |
| 512 MB / 8 vCPU | 3 | 390 MB | **memory** |
| 8 GB / 8 vCPU | 7 | 750 MB | cpu |

`http_threads` derives from workers (4 each, capped at 32) unless set: each HTTP
thread can hold a whole request body, so it is a memory knob too.

An explicit `BLURD_WORKERS=N` is always honoured — it is the operator's call —
but startup warns with the numbers if it will not fit, rather than letting them
find out from the OOM killer mid-job. `blurd doctor` reports the whole budget.

## Returning memory to the OS

glibc keeps freed blocks in per-thread arenas rather than returning them, so a
process that decodes a 10 MP image and frees it keeps the resident size anyway.

Three strategies, measured at 4000 px with 4 workers:

| | peak RSS | throughput |
|---|---:|---:|
| nothing | 788 MB | 9.8 img/s |
| **`malloc_trim()` between jobs** (default) | **613 MB** (−22%) | **9.3 img/s** (−5%) |
| `MALLOC_TRIM_THRESHOLD_=131072` | 513 MB (−35%) | 8.7 img/s (−11%) |

The default trims **between jobs**, not on every `free()`. It gets most of the
benefit at a fifth of the cost, because the moment nothing is in flight is the
cheap moment to hand pages back. `BLURD_MALLOC_TRIM=0` disables it.

The two stack — trim plus the threshold gives 510 MB — so the environment
variable remains worth setting where memory is genuinely the binding constraint
and 11% throughput is affordable. It is deliberately *not* the default, because
that trade is only right for the tightest deployments.

## The onnxruntime memory arena

onnxruntime keeps a CPU memory arena: freed tensors are retained for reuse
rather than returned. That is the right trade on a dedicated inference box and
the wrong one here, because on a small VM the arena is simply resident memory
that never comes back.

Measured, 4000 px, same build, arena the only variable:

| workers | arena on | arena off | saved |
|---:|---:|---:|---:|
| 1 | 240 MB | 192 MB | −20% |
| 4 | 618 MB | 416 MB | −33% |
| 7 | 961 MB | 672 MB | −30% |

Throughput was within run-to-run noise at every point — individual runs landed
on both sides (4w: 7.5 against 5.8 img/s; 7w: 6.9 against 7.6). There is no
consistent throughput cost to pay for the 30%.

The decisive test is a memory-capped container, because that is where the
difference stops being a number and becomes an outcome. 30 × 10.7 MP images at
8 concurrent, into a 512 MB container, both sized by `auto` to the same 3
workers:

| configuration | outcome |
|---|---|
| `BLURD_ORT_ARENA=1` | **OOM-killed**, 3/30 jobs completed |
| `BLURD_ORT_ARENA=0` (the default) | **30/30 completed**, 6.9 img/s |

So `ort_arena` defaults to off. An operator who turns it back on must also set
`workers` explicitly: `auto` derives from constants that assume it is off, and
will size ~40 MB per worker too generously.

## The queue is a memory consumer

Queued uploads are held in RAM. That is deliberate and load-bearing: blurd
promises never to store originals, so it does not spool source bytes to disk
(`spec/jobs-and-codes.md`). It also means the queue is part of the resident
footprint — and until 0.15.0 it was bounded by **job count alone**.

A job-count bound promises nothing about memory. The default was 1000 jobs; at
the measured ~1.5 MB per upload that is **~1.5 GB**, on a box the model above
believes needs 750 MB. Nothing in the sizing model knew about it, so the first
symptom would have been an OOM kill — which reads as a crash, not as a capacity
limit.

There are now two bounds, because they guard different things:

| bound | guards | default |
|---|---|---|
| `queue_max` | number of queued jobs | 1000 |
| `queue_max_bytes` | memory those jobs hold | `auto` |

`auto` is derived, not guessed: whatever is left of the budget once the workers
are paid for, times `QUEUE_SHARE` (0.5), clamped to [16, 512] MB. A `url` job
consumes a slot and no bytes; an upload consumes both — which is why neither
bound replaces the other.

| VM | workers | queue budget | modelled peak | usable |
|---|---:|---:|---:|---:|
| 512 MB | 3 | 22 MB | 412 MB | 435 MB |
| 1 GB | 7 | 60 MB | 810 MB | 870 MB |
| 2 GB | 7 | 495 MB | 1245 MB | 1740 MB |
| 8 GB | 7 | 512 MB | 1262 MB | 6963 MB |

Worker sizing now takes `QUEUE_MIN_MB` off the top first, so the two bounds
cannot jointly promise more memory than exists — which is precisely what they
did before.

**Two rules that are not obvious:**

- **An upload arriving at an empty queue is always admitted, whatever its
  size.** `MAX_BODY` is 64 MB and a 512 MB box's budget is 22 MB, so without
  this rule a large body would be rejected forever and retrying could never
  help. The cost is that one oversized payload may exceed the budget; the
  alternative is a producer that can never make progress.
- **The floor is 16 MB, not 64 MB.** Sizing a small box to reserve a whole
  `MAX_BODY` would have cost it a worker, to guard a case the admission rule
  already handles.

Overflow is **backpressure, not an error**: HTTP 503, code 108 (`overloaded`),
with `retry_after`. Deliberately not the old 106/502 — 502 means "upstream
returned garbage", and balancers eject a backend on repeated 502s, which is the
opposite of what a queue shedding load wants.

Exercised by `tests/queue_bytes.py`, including the invariant that matters most:
**the byte counter returns to zero once the queue drains.** A pop that forgot to
decrement would leak budget until the queue refused everything, and the process
would look perfectly healthy the whole time. `_release_payload` is the only
place `_payloads` shrinks, so there is no second path to forget.

## What has not been done

- **Reducing the copies themselves.** Redaction is now in place
  (`redact.apply(..., inplace=True)`), which removes a ~32 MB copy per 10 MP
  image, and the decode and redacted buffers are dropped explicitly before the
  encode. **This produced no measurable change in peak RSS** — repeated runs
  landed inside the noise, and `tracemalloc` accounts for only 45 MB of a job
  because numpy and cv2 allocate outside Python's allocator. The change is kept
  because it is strictly less work, but the copy is demonstrably not where the
  memory goes. The arena was.
- **The image is 464 MB**, almost entirely onnxruntime.
