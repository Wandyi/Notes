#!/usr/bin/env python3
"""
capacity.py - every number in the Lodestar design, derived in one place.

Run:   python3 capacity.py
       python3 capacity.py --verbose     (prints every intermediate value)

The process exits non-zero if any derived number drifts from the value the docs quote.
That is the point of the file: a doc and this arithmetic cannot silently disagree.

Stdlib only. Constants marked ASSUMPTION are inputs you should replace with your own
measurements. Everything else is arithmetic on those inputs.
"""

from __future__ import annotations

import math
import random
import sys

VERBOSE = "--verbose" in sys.argv
_failures: list[str] = []


def check(name: str, actual: float, expected: float, rel_tol: float = 0.01) -> None:
    """Record a drift if `actual` is not within rel_tol of the number the docs quote."""
    ok = math.isclose(actual, expected, rel_tol=rel_tol, abs_tol=1e-9)
    if VERBOSE or not ok:
        flag = "ok  " if ok else "DRIFT"
        print(f"  [{flag}] {name}: {actual:,.4f} (docs say {expected:,.4f})")
    if not ok:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n== {title}")


# ---------------------------------------------------------------------------------------
# 1. Corpus and storage                                                     (doc 01, doc 06)
# ---------------------------------------------------------------------------------------
DOCS = 10_000_000
MEAN_TOKENS_PER_DOC = 4_800          # ASSUMPTION: measured on a 50k-document sample
CHUNK_TARGET_TOKENS = 400            # design choice, doc 04
TOKENS_PER_SENTENCE = 22             # ASSUMPTION: corpus mean, all three languages
PAGES_PER_DOC = 9.6                  # ASSUMPTION: 500 tokens per page
DIM = 1024                           # bge-m3-class multilingual embedder
HNSW_M = 16
HNSW_BYTES_PER_NODE = 150            # 2m links x 4 B at layer 0, upper layers, list overhead
PAYLOAD_BYTES_PER_POINT = 150        # doc_id, version, dept, type, dates, lang, visibility
VECTOR_SHARDS = 8
VECTOR_RF = 3                        # one replica per availability zone
CHUNK_TEXT_BYTES = {"en": 1_680, "es": 1_600, "hi": 3_000}   # 400 tokens of each language
CORPUS_LANG_MIX = {"en": 0.70, "es": 0.18, "hi": 0.12}       # ASSUMPTION
CHUNK_META_BYTES = 700               # sentence offsets, page map, breadcrumb, ids
INVERTED_INDEX_RATIO = 0.45          # ASSUMPTION: Lucene postings + positions vs raw text

section("1. Corpus and storage")
chunks_per_doc = MEAN_TOKENS_PER_DOC / CHUNK_TARGET_TOKENS
chunks = DOCS * chunks_per_doc
sentences_per_chunk = CHUNK_TARGET_TOKENS / TOKENS_PER_SENTENCE
sentences = chunks * sentences_per_chunk
check("chunks per doc", chunks_per_doc, 12)
check("total chunks", chunks, 120_000_000)
check("sentences per chunk", sentences_per_chunk, 18.18)
check("total sentences (billions)", sentences / 1e9, 2.18)

f32_gb = chunks * DIM * 4 / 1e9
int8_gb = chunks * DIM * 1 / 1e9
graph_gb = chunks * HNSW_BYTES_PER_NODE / 1e9
payload_gb = chunks * PAYLOAD_BYTES_PER_POINT / 1e9
ram_per_copy_gb = int8_gb + graph_gb + payload_gb
ram_per_shard_gb = ram_per_copy_gb / VECTOR_SHARDS
check("float32 vectors (GB)", f32_gb, 491.52)
check("int8 vectors (GB)", int8_gb, 122.88)
check("HNSW graph (GB)", graph_gb, 18.0)
check("RAM per full copy (GB)", ram_per_copy_gb, 158.88)
check("RAM per shard (GB)", ram_per_shard_gb, 19.86)
check("RAM across RF=3 (GB)", ram_per_copy_gb * VECTOR_RF, 476.64)
check("float32 on NVMe across RF=3 (TB)", f32_gb * VECTOR_RF / 1e3, 1.475)

text_bytes = sum(CHUNK_TEXT_BYTES[k] * CORPUS_LANG_MIX[k] for k in CORPUS_LANG_MIX)
chunk_store_gb = chunks * (text_bytes + CHUNK_META_BYTES) / 1e9
raw_text_gb = chunks * text_bytes / 1e9
lexical_primary_gb = raw_text_gb * INVERTED_INDEX_RATIO
check("mean chunk text bytes", text_bytes, 1_824)
check("chunk store in Postgres (GB)", chunk_store_gb, 302.88)
check("raw chunk text (GB)", raw_text_gb, 218.88)
check("lexical index, primaries (GB)", lexical_primary_gb, 98.5)

# float32 at 1536 dims, no quantisation, which is what a naive design keeps in RAM
naive_f32_1536_gb = chunks * 1536 * 4 / 1e9
check("naive 1536-d float32 in RAM (GB)", naive_f32_1536_gb, 737.28)

# ---------------------------------------------------------------------------------------
# 2. Traffic                                                                       (doc 01)
# ---------------------------------------------------------------------------------------
EMPLOYEES = 120_000
DAU_FRACTION = 0.40                  # ASSUMPTION
QUERIES_PER_DAU = 5                  # ASSUMPTION
MACHINE_QUERIES_PER_DAY = 260_000    # ASSUMPTION: helpdesk copilot, chat bots, agents
PEAK_HOUR_SHARE = 0.12               # ASSUMPTION: IST afternoon overlaps CET morning
BURST_FACTOR = 2.0                   # ASSUMPTION: second-level peak over peak-hour mean
CAPACITY_HEADROOM = 3.0              # design choice
FOLLOWUP_SHARE = 0.35                # ASSUMPTION
ANSWER_CACHE_HIT = 0.10              # ASSUMPTION, to be measured, doc 12

section("2. Traffic")
human_q = EMPLOYEES * DAU_FRACTION * QUERIES_PER_DAU
total_q = human_q + MACHINE_QUERIES_PER_DAY
peak_hour_qps = total_q * PEAK_HOUR_SHARE / 3600
burst_qps = peak_hour_qps * BURST_FACTOR
capacity_qps = burst_qps * CAPACITY_HEADROOM
check("human queries/day", human_q, 240_000)
check("total queries/day", total_q, 500_000)
check("peak-hour mean QPS", peak_hour_qps, 16.67)
check("burst QPS", burst_qps, 33.33)
check("provisioned capacity QPS", capacity_qps, 100.0)
check("daily mean QPS", total_q / 86_400, 5.787)

# ---------------------------------------------------------------------------------------
# 3. Tokens per query and LLM cost                                         (doc 01, doc 16)
# ---------------------------------------------------------------------------------------
SYSTEM_PROMPT_TOKENS = 1_100         # instructions, output grammar, two worked examples
CONTEXT_TOKENS = 3_000               # 5 chunks x 400 + 600 expansion + 270 ids + 130 headers
HISTORY_TOKENS = 250                 # condensed history, mean over all queries
QUESTION_TOKENS = 40
OUTPUT_TOKENS_MEAN = 150
PRICE_IN = 1.00 / 1e6                # ASSUMPTION: fast-tier model, $ per input token
PRICE_IN_CACHED = 0.10 / 1e6         # ASSUMPTION: cached-prefix read
PRICE_OUT = 5.00 / 1e6               # ASSUMPTION
CONDENSE_IN, CONDENSE_IN_CACHED, CONDENSE_OUT = 650, 400, 45
GRAY_ANSWER_SHARE = 0.18             # answers with at least one gray-zone claim, doc 08
GRAY_CLAIMS_PER_GRAY_ANSWER = 1.3
JUDGE_IN, JUDGE_OUT = 450, 10

section("3. Tokens and LLM cost per query")
context_parts = 5 * 400 + 600 + 270 + 130
check("context tokens (built up)", context_parts, CONTEXT_TOKENS)
input_tokens = SYSTEM_PROMPT_TOKENS + CONTEXT_TOKENS + HISTORY_TOKENS + QUESTION_TOKENS
check("input tokens per generation", input_tokens, 4_390)

gen_cost = (SYSTEM_PROMPT_TOKENS * PRICE_IN_CACHED
            + (input_tokens - SYSTEM_PROMPT_TOKENS) * PRICE_IN
            + OUTPUT_TOKENS_MEAN * PRICE_OUT)
condense_cost = FOLLOWUP_SHARE * (CONDENSE_IN_CACHED * PRICE_IN_CACHED
                                  + (CONDENSE_IN - CONDENSE_IN_CACHED) * PRICE_IN
                                  + CONDENSE_OUT * PRICE_OUT)
judge_cost = (GRAY_ANSWER_SHARE * GRAY_CLAIMS_PER_GRAY_ANSWER
              * (JUDGE_IN * PRICE_IN + JUDGE_OUT * PRICE_OUT))
llm_cost_per_answer = gen_cost + condense_cost + judge_cost
check("generation $/answer", gen_cost, 0.004150)
check("condensation $/query (amortised)", condense_cost, 0.000181)
check("gray-zone judge $/answer (amortised)", judge_cost, 0.000117)
check("LLM $/answer", llm_cost_per_answer, 0.004448)

# Abstentions are decided before generation (doc 08), so they cost no generation call.
ABSTAIN_BEFORE_GENERATION = 0.06     # ASSUMPTION: share of queries the sufficiency gate stops
generated = total_q * (1 - ANSWER_CACHE_HIT) * (1 - ABSTAIN_BEFORE_GENERATION)
llm_month = generated * llm_cost_per_answer * 30
check("generations/day", generated, 423_000)
check("LLM $/month", llm_month, 56_436, rel_tol=0.01)

# Throughput the provider must sustain at provisioned capacity
check("input tokens/s at capacity", capacity_qps * input_tokens, 439_000)
check("output tokens/s at capacity", capacity_qps * OUTPUT_TOKENS_MEAN, 15_000)

# ---------------------------------------------------------------------------------------
# 4. GPU sizing from first principles                                     (doc 06, doc 07)
# ---------------------------------------------------------------------------------------
# Forward FLOPs per token for a transformer encoder ~= 2 x non-embedding params
#                                                     + 4 x layers x seq_len x hidden
# The vocabulary table of a multilingual model is ~250k x hidden and is a lookup, not a
# matmul, so it is excluded. That is why a "568M" XLM-R-large model costs like a 302M one.
def encoder_nonembed_params(layers: int, hidden: int) -> float:
    return layers * 12 * hidden * hidden


def encoder_flops(seq: int, layers: int, hidden: int) -> float:
    per_token = 2 * encoder_nonembed_params(layers, hidden) + 4 * layers * seq * hidden
    return seq * per_token


L4_TFLOPS = 121e12                   # dense FP16 tensor
H100_TFLOPS = 989e12                 # dense FP16 tensor, SXM
MFU = 0.40                           # ASSUMPTION: batched ONNX/TensorRT inference
L4_HOURLY = 0.85                     # ASSUMPTION
H100_HOURLY = 6.00                   # ASSUMPTION
TARGET_UTIL = 0.65                   # leave a third for bursts and batching jitter

section("4. GPU sizing")
xlmr_large = encoder_nonembed_params(24, 1024)
check("XLM-R-large non-embedding params (M)", xlmr_large / 1e6, 301.99)

l4_eff = L4_TFLOPS * MFU
h100_eff = H100_TFLOPS * MFU

# Ingestion embedding: 400-token chunk + 30-token breadcrumb
embed_flops = encoder_flops(430, 24, 1024)
embed_per_s_l4 = l4_eff / embed_flops
bulk_gpu_hours = chunks / embed_per_s_l4 / 3600
check("embed FLOPs per chunk (GFLOP)", embed_flops / 1e9, 277.93)
check("chunks/s per L4", embed_per_s_l4, 174.1)
check("full re-embed, L4 GPU-hours", bulk_gpu_hours, 191.4)
check("full re-embed, $ of GPU", bulk_gpu_hours * L4_HOURLY, 162.7)
check("full re-embed wall-clock on 16 L4s (h)", bulk_gpu_hours / 16, 11.97)

# Reranking: three designs
def rerank_ms(cands: int, seq: int, eff: float) -> float:
    return cands * encoder_flops(seq, 24, 1024) / eff * 1000


naive_l4 = rerank_ms(50, 544, l4_eff)       # 50 candidates, 512-token chunk + 32 query
naive_h100 = rerank_ms(50, 544, h100_eff)
chosen_l4 = rerank_ms(40, 288, l4_eff)      # 40 candidates, passage truncated to 256
chosen_h100 = rerank_ms(40, 288, h100_eff)
check("rerank 50x544 on L4 (ms)", naive_l4, 369.2)
check("rerank 50x544 on H100 (ms)", naive_h100, 45.2)
check("rerank 40x288 on L4 (ms)", chosen_l4, 150.5)
check("rerank 40x288 on H100 (ms)", chosen_h100, 18.4)

h100_needed = math.ceil(capacity_qps * chosen_h100 / 1000 / TARGET_UTIL)
l4_needed = math.ceil(capacity_qps * chosen_l4 / 1000 / TARGET_UTIL)
check("H100s for rerank at capacity", h100_needed, 3)
check("L4s for rerank at capacity", l4_needed, 24)
check("rerank $/hour on H100", h100_needed * H100_HOURLY, 18.0)
check("rerank $/hour on L4", l4_needed * L4_HOURLY, 20.4)

# Verification NLI: mDeBERTa-v3-base-class, 12 layers x 768
nli_tokens_per_claim = 35 + 70 + 40           # claim, cited sentences, neighbours
nli_flops = 3.2 * encoder_flops(nli_tokens_per_claim, 12, 768)
rerank_flops = 40 * encoder_flops(288, 24, 1024)
check("NLI GFLOP per answer", nli_flops / 1e9, 81.32)
check("NLI ms per answer on L4", nli_flops / l4_eff * 1000, 1.68)
check("rerank-to-verify compute ratio", rerank_flops / nli_flops, 89.6)

# Query embedding is negligible: 32 tokens
check("query embed ms on L4", encoder_flops(32, 24, 1024) / l4_eff * 1000, 0.40)

# ---------------------------------------------------------------------------------------
# 5. Ingestion throughput                                                          (doc 05)
# ---------------------------------------------------------------------------------------
DOCS_PER_DAY = 100_000
SPIKE = 10
SCANNED_SHARE = 0.14                 # ASSUMPTION: docs needing OCR
DIGITAL_PAGE_CORE_S = 0.35           # ASSUMPTION: layout-aware parse
OCR_PAGE_CORE_S = 2.5                # ASSUMPTION: CPU OCR, Latin + Devanagari models
BULK_PARSE_CORES = 400

section("5. Ingestion")
docs_s = DOCS_PER_DAY / 86_400
spike_docs_s = docs_s * SPIKE
core_s_per_page = (1 - SCANNED_SHARE) * DIGITAL_PAGE_CORE_S + SCANNED_SHARE * OCR_PAGE_CORE_S
check("docs/s average", docs_s, 1.157)
check("chunks/s average", docs_s * chunks_per_doc, 13.89)
check("chunks/s at 10x spike", spike_docs_s * chunks_per_doc, 138.9)
check("L4s busy embedding at spike", spike_docs_s * chunks_per_doc / embed_per_s_l4, 0.80)
check("core-seconds per page (blended)", core_s_per_page, 0.651)
check("parse cores at spike", spike_docs_s * PAGES_PER_DOC * core_s_per_page, 72.33)
check("parse cores average", docs_s * PAGES_PER_DOC * core_s_per_page, 7.23)
# What the 100k/day actually is (doc 00 s3.11): mostly new versions of existing documents
UPDATES_PER_DAY, NEW_PER_DAY, DELETES_PER_DAY = 75_000, 25_000, 22_000
check("ingestion events/day", UPDATES_PER_DAY + NEW_PER_DAY, DOCS_PER_DAY)
check("net corpus growth per year (M docs)", (NEW_PER_DAY - DELETES_PER_DAY) * 365 / 1e6, 1.095)
check("days to fill 10M if all 100k were new", DOCS / DOCS_PER_DAY, 100)
replaced = UPDATES_PER_DAY * chunks_per_doc
tombstoned = (UPDATES_PER_DAY + DELETES_PER_DAY) * chunks_per_doc
check("chunk replacements/day", replaced, 900_000)
check("chunks tombstoned/day", tombstoned, 1_164_000)
check("tombstoned share of index per day (%)", 100 * tombstoned / chunks, 0.97)
check("days to 20% tombstones with no compaction", 0.20 / (tombstoned / chunks), 20.62)

# LLM-written context per chunk (doc 04 s10): the document is read once uncached, then once
# from cache per chunk, and each chunk gets ~60 tokens of generated context.
CTX_OUT_TOKENS = 60
ctx_per_doc = (MEAN_TOKENS_PER_DOC * PRICE_IN
               + chunks_per_doc * MEAN_TOKENS_PER_DOC * PRICE_IN_CACHED
               + chunks_per_doc * CTX_OUT_TOKENS * PRICE_OUT)
check("LLM chunk context $/doc", ctx_per_doc, 0.01416)
check("LLM chunk context, one-off for 10M docs ($k)", ctx_per_doc * DOCS / 1e3, 141.6)
check("LLM chunk context, ongoing $/month", ctx_per_doc * DOCS_PER_DAY * 30, 42_480)
check("...only for the 10% of docs with no usable headings, $/month",
      ctx_per_doc * DOCS_PER_DAY * 30 * 0.10, 4_248)

# How often an answer races a change to a document it cites (doc 09 s6)
ANSWER_WINDOW_S = 1.3                # question to last claim, p50
DOCS_CITED_PER_ANSWER = 1.5          # ASSUMPTION: pilot traffic
exposure = ANSWER_WINDOW_S * DOCS_CITED_PER_ANSWER          # doc-seconds per answer
upd_rate = UPDATES_PER_DAY / 86_400 / DOCS                  # per doc per second
del_rate = DELETES_PER_DAY / 86_400 / DOCS
check("answers racing an update, per year", upd_rate * exposure * 423_000 * 365, 26.1, rel_tol=0.01)
check("answers racing a deletion, per year", del_rate * exposure * 423_000 * 365, 7.67, rel_tol=0.01)

bulk_pages = DOCS * PAGES_PER_DOC
bulk_parse_h = bulk_pages * core_s_per_page / BULK_PARSE_CORES / 3600
check("bulk-load pages (M)", bulk_pages / 1e6, 96.0)
check("bulk-load parse wall-clock on 400 cores (h)", bulk_parse_h, 43.4)

# ---------------------------------------------------------------------------------------
# 6. Latency: Monte Carlo over the stage budget                                   (doc 12)
# ---------------------------------------------------------------------------------------
# Each stage is lognormal, pinned by its own p50 and p99. Summing per-stage p99s is the
# usual mistake: it assumes every stage is at its worst on the same request.
Z99 = 2.3263


def lognormal(p50: float, p99: float, rng: random.Random) -> float:
    sigma = math.log(p99 / p50) / Z99
    return p50 * math.exp(rng.gauss(0.0, sigma))


STAGES = {                           # (p50 ms, p99 ms)
    "gateway_auth": (12, 40),
    "embed_query": (12, 35),
    "vector_search": (30, 95),       # with hedged shard requests, doc 06
    "lexical_search": (35, 110),
    "fuse_hydrate_gate": (10, 30),
    "rerank": (40, 105),             # H100, 40 x 288
    "assemble": (8, 22),
    "llm_ttft": (300, 900),
    "verify_tail": (15, 45),
    "deliver": (15, 50),
}
CONDENSE_HOSTED = (200, 520)         # fast-tier hosted model, network round trip included
CONDENSE = (150, 400)                # chosen: 3B-class condenser on the reranker's H100s
OUT_TOKENS = (130, 300)
DECODE_TPS = (160, 110)              # median tokens/s, and the 1st-percentile slow rate
HEDGE_AFTER_MS = 700
DEADLINE_MS = 2_900
MAX_OUTPUT_TOKENS = 300
CACHE_HIT_PATH = (45, 120)


def one_request(rng: random.Random, followup: bool, cache_hit: bool,
                condense: tuple[float, float] = CONDENSE) -> tuple[float, bool, bool]:
    s = {k: lognormal(*v, rng) for k, v in STAGES.items()}
    if cache_hit:
        t = s["gateway_auth"] + lognormal(*CACHE_HIT_PATH, rng) + s["deliver"]
        return t, False, False
    t = s["gateway_auth"]
    if followup:
        t += lognormal(*condense, rng)
    t += s["embed_query"]
    t += max(s["vector_search"], s["lexical_search"])
    t += s["fuse_hydrate_gate"] + s["rerank"] + s["assemble"]
    ttft = s["llm_ttft"]
    hedged = ttft > HEDGE_AFTER_MS
    if hedged:
        ttft = min(ttft, HEDGE_AFTER_MS + lognormal(*STAGES["llm_ttft"], rng))
    t += ttft
    tokens = min(lognormal(*OUT_TOKENS, rng), MAX_OUTPUT_TOKENS)
    # decode speed: slow tail means low tokens/s, so invert the lognormal around the median
    tps = DECODE_TPS[0] / lognormal(1.0, DECODE_TPS[0] / DECODE_TPS[1], rng)
    decode = tokens / tps * 1000
    tail = s["verify_tail"] + s["deliver"]
    truncated = t + decode + tail > DEADLINE_MS
    if truncated:
        decode = max(0.0, DEADLINE_MS - t - tail)
    return t + decode + tail, hedged, truncated


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


section("6. Latency Monte Carlo (200,000 requests)")
rng = random.Random(20261021)
N = 200_000
all_t, first_t, follow_t = [], [], []
hedged_n = truncated_n = generated_n = 0
for _ in range(N):
    fu = rng.random() < FOLLOWUP_SHARE
    hit = (not fu) and rng.random() < ANSWER_CACHE_HIT / (1 - FOLLOWUP_SHARE)
    t, h, tr = one_request(rng, fu, hit)
    all_t.append(t)
    (follow_t if fu else first_t).append(t)
    if not hit:
        generated_n += 1
        hedged_n += h
        truncated_n += tr

p50_all, p99_all = pct(all_t, 0.50), pct(all_t, 0.99)
check("end-to-end p50 (ms)", p50_all, 1_338, rel_tol=0.03)
check("end-to-end p99 (ms)", p99_all, 2_593, rel_tol=0.03)
check("first-turn p50 (ms)", pct(first_t, 0.50), 1_242, rel_tol=0.03)
check("follow-up p50 (ms)", pct(follow_t, 0.50), 1_490, rel_tol=0.03)
check("follow-up p99 (ms)", pct(follow_t, 0.99), 2_713, rel_tol=0.03)
check("share of generations hedged (%)", 100 * hedged_n / generated_n, 3.58, rel_tol=0.08)
check("share of generations truncated (%)", 100 * truncated_n / generated_n, 0.29, rel_tol=0.35)

# A hedged request pays for a second prefill (and a little output before it is cancelled)
hedge_input_cost = (SYSTEM_PROMPT_TOKENS * PRICE_IN_CACHED
                    + (input_tokens - SYSTEM_PROMPT_TOKENS) * PRICE_IN)
hedge_month = (hedged_n / generated_n) * hedge_input_cost * generated * 30
check("hedging cost $/month", hedge_month, 1_545, rel_tol=0.02)

# The same follow-up path with a hosted condenser instead, for the doc 12 comparison
rng_h = random.Random(20261021)
hosted = sorted(one_request(rng_h, True, False, CONDENSE_HOSTED)[0] for _ in range(100_000))
check("follow-up p50 with hosted condenser (ms)", pct(hosted, 0.50), 1_543, rel_tol=0.03)

sum_of_p99 = (sum(v[1] for v in STAGES.values()) - min(STAGES["vector_search"][1],
              STAGES["lexical_search"][1]) + CONDENSE[1]
              + MAX_OUTPUT_TOKENS / DECODE_TPS[1] * 1000)
check("naive sum of stage p99s, follow-up (ms)", sum_of_p99, 4_464, rel_tol=0.01)

# The article-style pipeline: HyDE call + 50x544 rerank on L4 + no output cap, first turn
NAIVE_EXTRA_P50 = 450 + (naive_l4 - 40)            # HyDE + slower reranker
check("naive pipeline p50 overrun (ms)", NAIVE_EXTRA_P50, 779.2)

# ---------------------------------------------------------------------------------------
# 7. Scatter-gather tail amplification                                            (doc 06)
# ---------------------------------------------------------------------------------------
section("7. Scatter-gather")
p_any_slow = 1 - 0.99 ** VECTOR_SHARDS
per_shard_needed = 0.99 ** (1 / VECTOR_SHARDS)
check("P(at least one of 8 shards past its p99) (%)", 100 * p_any_slow, 7.73)
check("per-shard percentile needed for a query p99", 100 * per_shard_needed, 99.874)

# ---------------------------------------------------------------------------------------
# 8. Availability                                                                  (doc 16)
# ---------------------------------------------------------------------------------------
A = {
    "gateway": 0.9999, "query_service": 0.9999, "postgres": 0.9995,
    "vector": 0.9995, "lexical": 0.9995, "reranker": 0.9995,
    "llm_provider": 0.999, "nli": 0.9995,
}
section("8. Availability")
serial = math.prod(A.values())
retrieval_either = 1 - (1 - A["vector"]) * (1 - A["lexical"])
minimal = A["gateway"] * A["query_service"] * A["postgres"] * retrieval_either
check("everything in series (%)", 100 * serial, 99.630, rel_tol=1e-4)
check("series downtime (hours/year)", (1 - serial) * 8766, 32.43, rel_tol=0.01)
check("minimal degraded path (%)", 100 * minimal, 99.930, rel_tol=1e-4)
check("minimal path downtime (hours/year)", (1 - minimal) * 8766, 6.14, rel_tol=0.01)
check("99.9% budget (hours/year)", 0.001 * 8766, 8.77, rel_tol=0.01)

# ---------------------------------------------------------------------------------------
# 9. Monthly cost                                                                  (doc 16)
# ---------------------------------------------------------------------------------------
# Production M1 measurement (doc 14 s6): 2% sample, strong judge of another family
M1_SAMPLE_CLAIMS_PER_DAY = 28_800
STRONG_IN, STRONG_OUT = 3.00 / 1e6, 15.00 / 1e6      # ASSUMPTION: strong-tier judge prices
m1_judge_month = M1_SAMPLE_CLAIMS_PER_DAY * (550 * STRONG_IN + 20 * STRONG_OUT) * 30

H = 730
monthly = {
    "llm": llm_month,
    "rerank_gpu": h100_needed * H100_HOURLY * H,
    "online_l4_embed_and_nli": 3 * L4_HOURLY * H,
    "ingest_l4_autoscaled": 2 * L4_HOURLY * H,
    "vector_nodes": 12 * 0.69 * H,                # 64 GiB RAM + 1.8 TB NVMe class
    "lexical_nodes": 6 * 0.75 * H,
    "postgres": 4_000,
    "redis_kafka": 3 * 0.40 * H + 3 * 0.50 * H,
    "parse_cpu": 1_500,
    "object_store": 15 * 23,
    "observability": 3_000,
    "hedging": hedge_month,
    "chunk_context_10pct": ctx_per_doc * DOCS_PER_DAY * 30 * 0.10,
    "m1_production_judging": m1_judge_month,
}
section("9. Monthly cost")
total_month = sum(monthly.values())
llm_all = monthly["llm"] + monthly["hedging"] + monthly["chunk_context_10pct"] + monthly["m1_production_judging"]
check("rerank GPU $/month", monthly["rerank_gpu"], 13_140)
check("vector nodes $/month", monthly["vector_nodes"], 6_044)
check("M1 production judging $/month", monthly["m1_production_judging"], 1_685, rel_tol=0.01)
check("total $/month", total_month, 100_300, rel_tol=0.01)
check("generation LLM share of total (%)", 100 * monthly["llm"] / total_month, 56.3, rel_tol=0.01)
check("all model-provider spend share (%)", 100 * llm_all / total_month, 63.7, rel_tol=0.01)
check("all-in $/query", total_month / (total_q * 30), 0.00669, rel_tol=0.01)

# The same design at 5,000 queries per second (doc 16 s7), everything else equal
SCALE_QPS = 5_000
check("rerank H100s at 5,000 QPS", math.ceil(SCALE_QPS * chosen_h100 / 1000 / TARGET_UTIL), 142)
check("NLI L4s at 5,000 QPS", math.ceil(SCALE_QPS * nli_flops / l4_eff / TARGET_UTIL), 13)
check("provider input tokens/s at 5,000 QPS (M)", SCALE_QPS * input_tokens / 1e6, 21.95)
check("LLM $/month at 50x the daily volume (M)", 50 * llm_month / 1e6, 2.82, rel_tol=0.01)

# ---------------------------------------------------------------------------------------
# 10. Residual unsupported claims                                                  (doc 08)
# ---------------------------------------------------------------------------------------
CLAIMS_PER_ANSWER = 3.2
P_UNSUPPORTED_RAW = 0.030            # ASSUMPTION: measured on the golden set
DET_CATCH = 0.45                     # deterministic checks catch this share of unsupported


def residual(nli_false_accept: float, nli_gray: float, judge_false_accept: float) -> float:
    after_det = P_UNSUPPORTED_RAW * (1 - DET_CATCH)
    return after_det * (nli_false_accept + nli_gray * judge_false_accept)


def answers_affected(per_claim: float) -> float:
    return 1 - (1 - per_claim) ** CLAIMS_PER_ANSWER


section("10. Residual unsupported claims")
r_base = residual(0.06, 0.14, 0.10)
r_tuned = residual(0.03, 0.10, 0.10)
check("off-the-shelf verifier, claims escaping (%)", 100 * r_base, 0.1221)
check("off-the-shelf, answers with >=1 bad claim (%)", 100 * answers_affected(r_base), 0.390)
check("off-the-shelf, bad answers/day", answers_affected(r_base) * generated, 1_650, rel_tol=0.01)
check("fine-tuned verifier, claims escaping (%)", 100 * r_tuned, 0.066)
check("fine-tuned, answers with >=1 bad claim (%)", 100 * answers_affected(r_tuned), 0.211)
check("fine-tuned, bad answers/day", answers_affected(r_tuned) * generated, 892, rel_tol=0.01)

# Strict mode (doc 08): queries whose evidence is legal, compliance or HR policy get
# extractive answers - verbatim sentences under a fixed template - so no paraphrase exists.
STRICT_SHARE = 0.22                  # ASSUMPTION: measured share of queries hitting those classes
check("fine-tuned + strict mode, bad answers/day",
      answers_affected(r_tuned) * generated * (1 - STRICT_SHARE), 696, rel_tol=0.01)

# Outcome mix (doc 02 s6): what the 500k queries a day end as
FALSE_REJECT_TUNED = 0.02
p_drop = (P_UNSUPPORTED_RAW - r_tuned) + (1 - P_UNSUPPORTED_RAW) * FALSE_REJECT_TUNED
p_partial = answers_affected(p_drop)
strict_n = generated * STRICT_SHARE
generative_n = generated - strict_n
partial_n = generative_n * p_partial
verified_n = generative_n - partial_n
check("per-claim drop probability (%)", 100 * p_drop, 4.874)
check("generative answers with a dropped claim (%)", 100 * p_partial, 14.78)
check("outcome: verified (% of queries)", 100 * verified_n / total_q, 56.23)
check("outcome: partial (% of queries)", 100 * partial_n / total_q, 9.75)
check("outcome: extractive by strict mode (% of queries)", 100 * strict_n / total_q, 18.61)
check("outcome: abstained (% of queries)",
      100 * total_q * (1 - ANSWER_CACHE_HIT) * ABSTAIN_BEFORE_GENERATION / total_q, 5.40)

# The arithmetic that rules out "just use a better model"
CLAIMS_PER_DAY = generated * CLAIMS_PER_ANSWER
check("claims generated per day", CLAIMS_PER_DAY, 1_353_600)
check("unsupported claims/day at 99.9% faithfulness", CLAIMS_PER_DAY * 0.001, 1_353.6)

# The cost of strictness: supported claims wrongly dropped
def wrongly_dropped_answers(false_reject: float) -> float:
    per_claim = (1 - P_UNSUPPORTED_RAW) * false_reject
    return answers_affected(per_claim)


check("off-the-shelf, answers losing a good claim (%)", 100 * wrongly_dropped_answers(0.04), 11.89)
check("fine-tuned, answers losing a good claim (%)", 100 * wrongly_dropped_answers(0.02), 6.08)

# ---------------------------------------------------------------------------------------
print()
if _failures:
    print(f"DRIFT in {len(_failures)} number(s): {', '.join(_failures)}")
    sys.exit(1)
print("capacity.py: every number matches the docs.")
