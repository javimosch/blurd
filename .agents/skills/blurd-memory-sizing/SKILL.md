---
name: blurd-memory-sizing
description: How blurd's memory is actually spent, how to measure it without being misled, and why the sizing constants are what they are. Read before changing src/resources.py, the worker/queue defaults, the pipeline's buffers, or anything that claims to reduce memory. Includes the onnxruntime arena finding and the measurement methods that do NOT work here.
---

# Memory and sizing

blurd targets cheap CPU-only VMs, so memory is the binding constraint, not
cores. Everything here was measured on this pipeline; none of it is inherited
wisdom.

## The model

```
peak resident  ~=  120 MB  +  90 MB x workers  +  queue budget
```

Fitted to 4000 px — the costly case, so the one to plan with. The constants in
`src/resources.py` sit deliberately **above** the fit: guessing low costs an OOM
kill, guessing high costs an idle core.

Sizing is **cgroup-aware**. Inside a container the host's core count and total
memory are fictions; `memory.max` and `cpu.max` are the truth. Never size
anything from `os.cpu_count()` alone.

> **The constants assume `ort_arena` is off.** Turning it back on adds ~40 MB
> per worker, and `auto` will then size too generously. A deployment that
> enables it must also set `workers` explicitly.

## The finding: the arena, not the copies

The obvious suspect was the pipeline's buffers — a full-size decode, the
detection downscale, the redacted copy and the encode buffer are all live at
once. **That hypothesis was wrong, and the measurement said so.**

Making redaction write in place removed a ~32 MB copy per 10 MP image and
produced **no measurable change** in peak RSS. Repeated runs landed inside the
noise. The change was kept because it is strictly less work, but the copy was
not where the memory went.

It was **onnxruntime's CPU memory arena**, which retains freed tensors for
reuse. That is the right trade on a dedicated inference box and the wrong one
on a small VM, where it is simply resident memory that never comes back.

| workers, 4000 px | arena on | arena off |
|---:|---:|---:|
| 1 | 240 MB | 192 MB |
| 4 | 618 MB | 416 MB |
| 7 | 961 MB | 672 MB |

Throughput differences landed on **both sides** across runs — noise, not a
trade. Decisive test, 512 MB container, 30 × 10.7 MP at 8 concurrent, both
auto-sized to the same 3 workers: **arena on → OOM-killed at 3/30; arena off →
30/30**.

## How to measure — and what will mislead you

Three methods were tried. Only one answers the question.

- **`tracemalloc` is blind here.** It accounted for 45 MB of a job that moves
  hundreds. numpy and cv2 allocate outside Python's allocator, so Python-level
  profiling sees almost none of the real cost. Do not conclude "there is no
  problem" from a clean tracemalloc.
- **Single-job RSS sampling is too noisy to decide anything.** Baselines drift
  30 MB between runs, so a 30 MB effect is unmeasurable this way. It produced
  flatly contradictory answers on the in-place change.
- **`bench/memory.sh` at several worker counts, plus a memory-capped
  container, is the method that works.** The container turns a noisy number
  into a binary outcome — killed or not killed — and that is what a sizing
  decision actually needs.

**Do not claim a memory win that only shows up as a smaller number in one run.**
If it does not change what a capped container does, it is noise.

## Two knobs, and why the second is not the first

`malloc_trim()` between jobs is on by default: glibc holds freed blocks in
per-thread arenas rather than returning them. It is deliberately **not** the
same as lowering `MALLOC_TRIM_THRESHOLD_`, which was measured at −37 % memory
for −18 % throughput because it trims on every free, including inside the hot
loop. Trimming once per job gets the memory back at a moment when nothing is in
flight.

## The queue is a memory consumer

Queued uploads live in RAM by design — blurd promises never to store originals,
so it does not spool source bytes to disk. That makes the queue part of the
resident footprint, and it was once bounded by **job count alone**, which
promises nothing about memory: 1000 jobs × ~1.5 MB is ~1.5 GB on a box the
model believes needs 750 MB.

Two bounds now, because a `url` job consumes a slot and no bytes while an upload
consumes both:

| bound | guards | default |
|---|---|---|
| `queue_max` | queued jobs | 1000 |
| `queue_max_bytes` | memory they hold | `auto` |

`auto` derives from what is left after the workers are paid for, and worker
sizing takes `QUEUE_MIN_MB` off the top first — so the two bounds cannot
jointly promise more memory than exists, which is exactly what they did before.

Two rules that are not obvious:

- **An upload arriving at an empty queue is always admitted, whatever its
  size.** `MAX_BODY` is 64 MB and a 512 MB box's budget is 22 MB; without this,
  a large body would be rejected forever and retrying could never help.
- **The floor is 16 MB, not 64 MB.** Reserving a whole `MAX_BODY` would cost a
  small box a worker, to guard a case the admission rule already handles.

Overflow is backpressure: **HTTP 503, code 108 (`overloaded`)**, with
`retry_after`. Not 502 — that means "upstream returned garbage", and balancers
eject a backend on repeated 502s, the opposite of what a queue shedding load
wants.

## If you change what the pipeline holds

Re-run `bash bench/memory.sh`, update the constants in `src/resources.py`, and
update the table in `spec/resources.md`. The constants are fitted to
measurement, not derived from theory; leaving them stale is how a deployment
gets OOM-killed by a default that used to be right.
