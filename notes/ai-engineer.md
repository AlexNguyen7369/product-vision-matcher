# Becoming a Better AI Engineer

A practical rundown on building agents, optimizing token usage, and leveling up
as an AI engineer.

## 1. How to build agents

An "agent" is just a loop: **model → tool call → observe result → repeat until
done.** Everything else is engineering around that loop.

### The core loop
- Give the model a goal, a set of tools, and let it decide which tool to call.
- Execute the tool, feed the result back into context, and let it decide the
  next step.
- Stop when the model emits a final answer (or you hit a guardrail like
  max-iterations).

### Design principles that actually matter
- **Tools are your API surface.** Each tool needs a crisp name, a tight
  description, and a typed schema. The model is only as good as your tool
  descriptions — write them like documentation for a junior dev. Ambiguous tool
  descriptions cause more failures than weak models.
- **Start with the simplest thing that works.** A single model + a few tools
  beats an elaborate multi-agent graph for most tasks. Add orchestration only
  when a single loop demonstrably can't handle the work.
- **Single agent first, multi-agent later.** Multi-agent (orchestrator +
  subagents) is worth it when you need *parallel exploration* (fan out searches)
  or *context isolation* (each subagent gets a clean window). It costs more
  tokens and adds coordination failure modes — don't reach for it by default.
- **Make failures recoverable.** Tools should return useful errors ("file not
  found at X, did you mean Y?") so the model can self-correct instead of
  dead-ending.
- **Verification loops beat one-shot.** Let the agent check its own work — run
  tests, re-read output, compare against the goal. Agents that can observe
  consequences and retry vastly outperform fire-and-forget.

### A maturity ladder
1. Single prompt, no tools.
2. Prompt + retrieval (RAG) for grounded answers.
3. Prompt + tools (the basic agent loop).
4. Multi-step agent with planning + verification.
5. Multi-agent orchestration (only when 1–4 genuinely fall short).

Climb only as high as the task requires.

## 2. Optimizing token usage

Tokens are your latency, cost, *and* quality budget — bloated context degrades
reasoning, not just your bill.

### Biggest levers, in order of impact
- **Prompt caching.** The single highest-ROI optimization. Put your stable
  content (system prompt, tool definitions, large reference docs, few-shot
  examples) at the *front* of the prompt and mark it cacheable. Cache reads are
  ~10% the cost of fresh input tokens. Keep the volatile part (the user's
  current turn) at the end so the prefix stays cache-stable. Note the cache has
  a ~5-minute TTL — back-to-back calls hit it, long gaps miss.
- **Retrieve, don't dump.** Don't stuff an entire codebase/document into
  context. Use search/embeddings to pull only the relevant slices. A targeted
  2K-token excerpt beats a 100K-token dump on both cost and accuracy.
- **Context isolation via subagents.** Spin off a subagent to do a noisy task
  (sweep 50 files, read logs) and return only the *conclusion*. The parent's
  context stays clean — the 50 file dumps never enter it.
- **Compact long histories.** In long-running loops, summarize old turns rather
  than carrying raw transcripts. Keep recent turns verbatim, compress the tail.
- **Right-size the model.** Use a smaller/faster model for classification,
  routing, and extraction; reserve the frontier model for hard reasoning. A
  cheap model behind a good prompt handles most sub-tasks.
- **Constrain outputs.** Ask for structured/minimal output. "Return just the
  file path" saves a paragraph of preamble each call, multiplied across
  thousands of calls.

### Measure before optimizing
- Use token-counting endpoints to profile where tokens actually go *before* you
  optimize. Usually it's one bloated tool result or a giant system prompt, not
  what you'd guess.
- Track input vs. output vs. cache-read tokens separately — they have very
  different costs.

## 3. Becoming a better, more optimized AI engineer

### Mindset shifts that separate good from great
- **Evals are your real skill.** Anyone can write a prompt. The engineers who
  ship reliable AI build *evaluation sets* — concrete input/expected-output
  pairs — and measure changes against them. Without evals you're tuning blind.
  Start a 20-example eval set on day one of any serious project.
- **Treat prompts as code.** Version them, diff them, review them. A prompt
  change is a deploy. "It seems better" is not a result; "pass rate went
  72% → 89% on the eval set" is.
- **Read the failures.** Spend more time reading transcripts of what went wrong
  than admiring what went right. Failure analysis is where the real improvements
  come from.
- **Understand the model's actual behavior**, not folklore. Test claims yourself
  ("does adding this line help?") rather than cargo-culting prompt tricks.

### Concrete habits
- Build an eval harness early; automate it.
- Instrument everything — log tokens, latency, tool-call traces, and failure
  reasons.
- Prefer determinism where you can get it: structured outputs, schemas,
  validation on tool results.
- Keep a "prompt changelog" so you know which change moved which metric.
- Learn the cost model cold — caching, batch processing, model tiers — so
  optimization is reflexive.

### A learning path
1. Master single-call prompting + structured outputs.
2. Build evals and learn to read traces.
3. Add tool use and build a basic agent loop by hand (don't start with a
   framework — understand the loop first).
4. Learn retrieval/RAG and context engineering.
5. Then adopt frameworks (or build thin ones) once you know what they're
   abstracting.
6. Tackle multi-agent and production concerns: caching, observability, cost,
   latency, safety.

---

**The through-line:** the best AI engineers obsess over **context engineering**
(what goes into the window and why) and **evaluation** (proving a change
helped). Model choice and prompt wording matter, but those two disciplines are
what compound.

> Caveat: specifics like exact cache pricing, model IDs, and API parameters
> change over time — verify current numbers against official docs before relying
> on them.
