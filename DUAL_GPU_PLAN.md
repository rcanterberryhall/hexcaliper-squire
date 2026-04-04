# Dual P40 GPU Analysis Architecture

## Hardware

Two NVIDIA Tesla P40 GPUs, 24 GB GDDR5X each, **~47 GB usable VRAM combined**
(firmware/driver overhead reduces addressable VRAM to approximately 23.6 GB per card).
Connected via PCIe 3.0 x16. There is no NVLink — inter-GPU communication goes through
the PCIe bus, so a single model spanning both GPUs will be slower per token than
a model that fits on one.

---

## VRAM Ceiling — The Key Constraint

The next model size up in the Qwen3 family is 72B, which requires approximately
44–46 GB at Q4_K_M quantization. With only ~47 GB usable across both GPUs, the
remaining 1–3 GB is insufficient for meaningful KV cache — qwen3:72b is not viable
on this hardware.

The value of spanning both GPUs is **extended context window, not model size.**

| Configuration | VRAM for weights | VRAM for KV cache | Practical context |
|---|---|---|---|
| qwen3:32b on one P40 | ~20 GB | ~3.6 GB | 4–8K tokens |
| qwen3:32b across both P40s | ~20 GB | ~27 GB | 32K+ tokens |

With both GPUs, qwen3:32b can process much longer documents and conversation
histories in a single pass — transforming what the model can reason over without
chunking or summarisation.

---

## Model Selection

### Daytime — Split by workload

| GPU | Port | Model | VRAM at Q4 | Primary consumer |
|-----|------|-------|-----------|-----------------|
| GPU 0 | 11434 | qwen3:32b | ~20 GB | Hexcaliper chat (interactive) |
| GPU 1 | 11435 | qwen3:8b | ~5 GB | Squire analysis (batch throughput) |

Each model fits on a single GPU with comfortable KV cache headroom. No PCIe
cross-talk during inference — both instances run fully independently.

GPU 0 carries interactive chat where reasoning quality matters. GPU 1 carries
higher-throughput analysis where speed matters more than depth. qwen3:8b is fast
enough to process many items per hour and reliable enough for structured JSON
extraction.

### Overnight — qwen3:32b in extended-context mode

One Ollama instance spanning both GPUs (`CUDA_VISIBLE_DEVICES=0,1`).
Same model binary as day-mode GPU 0 — no re-download required, only VRAM
re-allocation.

| Property | Value |
|---|---|
| Model | qwen3:32b |
| VRAM at Q4 | ~20 GB (weights) + up to ~27 GB (KV cache) |
| Instance count | 1 (both GPUs) |
| Context window | 32K+ tokens (configurable via `num_ctx`) |
| Throughput | Slower than single-GPU (PCIe overhead) — acceptable overnight |

The PCIe bandwidth penalty is irrelevant overnight. At reduced tokens/second,
a full re-analysis of several hundred items completes in a few hours — well
within an overnight window.

---

## VRAM Allocation — Mutual Exclusivity

The two modes are mutually exclusive:

```
Daytime mode:
  [GPU 0: qwen3:32b chat instance]   ~20 GB used
  [GPU 1: qwen3:8b  batch instance]   ~5 GB used

Overnight mode:
  [GPU 0 + GPU 1: qwen3:32b spanning both, extended context]   ~47 GB budget
```

Switching modes requires unloading the current model(s) before loading the next.
Ollama supports explicit model unloading via `keep_alive: 0` on a generate request.

---

## Daytime Pipeline — Parallel Analysis

### Ollama instance configuration

```bash
# Instance A — GPU 0 only (chat model)
CUDA_VISIBLE_DEVICES=0 ollama serve --port 11434

# Instance B — GPU 1 only (analysis model)
CUDA_VISIBLE_DEVICES=1 ollama serve --port 11435
```

### Squire (Parsival) changes required

1. **Raise the semaphore** from `Semaphore(1)` to `Semaphore(2)` in `app.py`
2. **Add a second Ollama URL** to config (`OLLAMA_URL_B = http://host.docker.internal:11435`)
3. **Round-robin dispatcher** — replace the single `OLLAMA_URL` with a small pool;
   each semaphore acquisition picks the next available URL

The LLM calls are stateless — each item goes in, JSON comes out, no shared state
between calls. The existing `db_lock` serialises all writes, so concurrent saves
from both workers are already safe.

---

## Overnight Pipeline — Extended-Context Re-Analysis

### Phase 1: Consolidate to single instance

```
1. Unload qwen3:32b from GPU 0 (keep_alive: 0)
2. Unload qwen3:8b  from GPU 1 (keep_alive: 0)
3. Stop Ollama-1 container
4. Restart Ollama-0 with CUDA_VISIBLE_DEVICES=0,1
5. Load qwen3:32b with num_ctx=32768 (or higher if KV budget allows)
```

### Phase 2: Batch re-analysis with long context

Rather than processing items one by one with 4K context, the overnight pass
can pass full document history, prior analysis, and situation context in a single
prompt — up to 32K tokens. This is the quality advantage: the model sees the
whole picture at once rather than fragments.

```
Worker processes items sequentially against the single extended-context instance.
Output is written to SQLite via the existing upsert logic.
```

Sequential processing is intentional — a single dual-GPU instance cannot
parallelise internally, and running multiple concurrent requests would thrash
the shared KV cache.

### Phase 3: Situation formation

Run situation formation **after** batch re-analysis completes, against the full
updated database. This is critical — situation formation looks across all items
to find clusters. Running it mid-batch would produce incomplete clusters.

### Phase 4: Return to daytime

```
1. Unload qwen3:32b from dual-GPU instance (keep_alive: 0)
2. Stop Ollama-0, restart with CUDA_VISIBLE_DEVICES=0
3. Start Ollama-1 with CUDA_VISIBLE_DEVICES=1
4. Preload qwen3:32b on GPU 0, qwen3:8b on GPU 1
```

### Sequencing summary

```
1. Unload daytime models
2. Reconfigure Ollama-0 to span both GPUs
3. Load qwen3:32b with extended num_ctx
4. Process pending items sequentially (full context per item)
5. Run situation formation (single pass, full DB)
6. Unload extended-context instance
7. Restore daytime configuration
```

---

## Scheduling

The overnight job trigger is a new `/reanalyze/deep` endpoint in the Parsival API that:

- Accepts a scheduled time parameter
- Handles the model swap (calls Ollama to unload/load)
- Runs the batch with extended context
- Runs situation formation on completion
- Swaps back to daytime configuration

Once merLLM is built (see `ROADMAP.md`), this orchestration moves into merLLM and
Parsival submits work via the batch API instead of managing the model swap itself.

---

## Why This Split Makes Sense

| Concern | Daytime 32B+8B split | Overnight 32B extended |
|---|---|---|
| Chat quality | Best possible (qwen3:32b) | N/A (night mode is batch only) |
| Batch throughput | Good (qwen3:8b, fast per token) | Slower but acceptable |
| Context per item | 4–8K tokens | 32K+ tokens |
| PCIe bottleneck | None (single-GPU each) | Present but irrelevant overnight |
| Situation synthesis | Adequate with chunked context | Much better — full history in one pass |
| Model size ceiling | Limited by 23.6 GB single-GPU | Same model, more context |

The 72B model class is not viable on this hardware. The extended-context approach
with qwen3:32b delivers the quality improvement for deep analysis without requiring
a larger model.
