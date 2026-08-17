This specification, architecture design, and phased implementation plan are tailored specifically for a **Go (Golang)** native implementation.

**Revision note:** this version closes ten gaps found in review — SCIP indexer orchestration, Memgraph persistence, incremental-ingestion correctness (deletes/renames/force-push), permission-sync freshness, merge/rerank design, query routing/cost control, embedding lifecycle, repo sharding, observability, and rollout/testing strategy. Changes are marked inline with **[UPDATED]** or **[NEW]**.

---

## 1. System Specification

### Functional Requirements

* **Go-Native Ingestion Worker:** Process Git webhooks (`push`, `PR merge`) using `go.temporal.io/sdk` to execute incremental `git diff` parsing asynchronously.
* **[UPDATED] Incremental Ingestion Correctness:** The diff processor must explicitly classify each changed path as `added`, `modified`, `deleted`, or `renamed` (via `git diff --find-renames`), and handle each differently:
  * `deleted` → remove the file's Zoekt shard entries, delete Qdrant points by `file_path` payload filter, delete the corresponding Memgraph symbol nodes/edges.
  * `renamed` → update `file_path` metadata in place across all three stores rather than delete+reinsert, to preserve chunk/embedding history and avoid needless re-embedding cost.
  * `added`/`modified` → standard chunk/embed/index pipeline.
  * **Force-push / history rewrite detection:** before processing a webhook diff, compare the previous indexed HEAD SHA (stored in Postgres) against the new event's parent SHA. If the parent doesn't match, the diff is non-linear — abandon incremental diffing and enqueue a full re-index for that repo instead of trusting the diff.
* **[NEW] SCIP Index Generation Orchestration:** SCIP indexes are not parsed from source generically — each language requires its own indexer binary (`scip-go`, `scip-typescript`, `scip-java`, `scip-python`, etc.). The ingestion worker invokes these as subprocesses/containerized jobs per repo/language, then the resulting `.scip` file is what `pkg/scip` reads and transforms into Memgraph nodes/edges. Languages without a mature SCIP indexer fall back to Tree-sitter-derived symbol edges only (lower fidelity — no cross-repo resolution), and this is surfaced to the UI as a per-language "index quality" flag rather than silently degraded.
* **Tri-Engine Retrieval Orchestration:**
  * **Trigram / Exact Search:** Embed `github.com/sourcegraph/zoekt` libraries directly or query Zoekt indexers via Go RPC/HTTP for sub-second regex and symbol matches.
  * **AST Semantic Search:** Parse code using `github.com/smacker/go-tree-sitter` (or official `go-tree-sitter`), embed chunks, and store/retrieve vectors via `github.com/qdrant/go-client`.
  * **Symbol Call Graph Traversal:** Query SCIP dependency trees in Memgraph using `github.com/neo4j/neo4j-go-driver/v5` via Cypher queries over Bolt. **[UPDATED]** Note explicitly: Memgraph is used here (not Neo4j) because it speaks the Bolt protocol and the Neo4j Go driver is Bolt-compliant — this is a deliberate substitution for licensing/deployment reasons, not an accidental driver mismatch. Document this in code comments and onboarding docs to avoid future confusion.
* **[UPDATED] Context Assembly & Reranking:** Execute parallel retrieval using Go `golang.org/x/sync/errgroup`, then run a **defined merge → rerank pipeline** (see §3.A) rather than an unspecified `mergeAndDeduplicate` stub, calling HuggingFace TEI for cross-encoder reranking on the merged candidate set.
* **[NEW] Query Routing / Engine Selection:** A lightweight upstream classifier decides which of the three engines a query actually needs (see §3.D) instead of always fanning out to all three. Structural/symbol-prefixed queries (`func:`, `class:`, exact-match regex) skip vector search; natural-language queries skip trigram search; symbol-navigation queries (go-to-def, find-refs) skip vector search entirely and go straight to the graph engine.
* **Multi-Tenant Gateway:** Validate Keycloak JWTs, extract tenant claims (`repo_id`), and automatically inject payload filters into Qdrant/Zoekt query contexts before execution.
* **[UPDATED] Permission Sync Freshness:** Polling-only sync (every 15 min) leaves a window where a de-provisioned user retains access — unacceptable for a regulated environment. The primary invalidation path is now **webhook-driven**: the Git host's repo-access-change event (collaborator removed, team membership changed, repo visibility changed) triggers immediate invalidation of that user/repo pair in a short-TTL cache (seconds, not minutes) sitting in front of the Postgres permissions table. The 15-minute poll becomes a **reconciliation fallback** only, catching anything the webhook missed (delivery failure, host doesn't emit the event, etc.), not the primary mechanism.
* **AI Agent Integration:** Expose an MCP server using `github.com/modelcontextprotocol/go-sdk` for Claude Code, Cursor, and custom application team agents.
* **[NEW] Embedding Model Versioning:** Embedding model upgrades are common and vectors from different model versions are not comparable. Qdrant collections are versioned (`chunks_v1`, `chunks_v2`, …); a migration job backfills the new collection via re-embedding rather than overwriting in place, and query-time routing points at the current active collection via config, with the old collection kept until backfill + validation completes.

### Non-Functional Requirements & Go Runtime Profile

| Metric | Target Specification | Go Implementation Strategy |
| --- | --- | --- |
| **Exact Search Latency** | <200 ms (10M+ LOC) | Direct memory-mapped Zoekt index reads with zero GC overhead. |
| **Hybrid RAG Latency** | <800 ms total query context | Concurrent goroutines with `errgroup` and context cancellation timeouts; **[UPDATED]** budget assumes query classifier has already pruned unneeded engines — see §3.D. |
| **Incremental Ingestion** | <15 s per commit (<50 files) | Parallel Cgo Tree-sitter parsing across all available CPU cores (`GOMAXPROCS`). |
| **Ingestion Lag (SLA-facing)** | **[NEW]** p95 < 60s from commit push to queryable | Measured end-to-end: webhook receipt timestamp → last store (Zoekt/Qdrant/Memgraph) write confirmed. Exposed as a Prometheus histogram, not inferred from the 15s/commit figure. |
| **Permission Propagation Lag** | **[NEW]** p95 < 5s from access-change event to enforcement | Webhook receipt → cache invalidation confirmed. |
| **Memory Footprint** | <500 MB RSS baseline per worker | Statically typed struct layouts, sync.Pool buffer recycling for AST allocations. |
| **Binary Deployment** | Single static executable | Compiled via `CGO_ENABLED=1 go build -ldflags="-w -s"` into minimal scratch/distroless container images. |

---

## 2. Go System Architecture & Package Layout

### High-Level Component Flow **[UPDATED — adds SCIP indexer subprocess step, embedding service, query classifier, permission cache/webhook path]**

```
                                [ Git Webhook: push / PR merge ]
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │   cmd/ingest (Temporal SDK)    │
                       │  diff classify: add/mod/del/   │
                       │  rename + force-push detection │
                       └───────────────┬───────────────┘
                                       │
     ┌─────────────────┬───────────────┼───────────────┬─────────────────────┐
     ▼                 ▼               ▼               ▼                     ▼
┌──────────┐   ┌────────────────┐ ┌──────────┐  ┌───────────────┐  ┌──────────────────┐
│ Zoekt    │   │ Tree-sitter AST│ │ SCIP     │  │ Embedding Svc  │  │ (deletes/renames  │
│ Indexer  │   │ Chunking (Cgo) │ │ Indexer  │  │ (HF TEI gRPC)  │  │  routed to all    │
│ (Go)     │   │                │ │ Runner   │  │  [NEW]         │  │  3 stores) [NEW]  │
└────┬─────┘   └───────┬────────┘ └────┬─────┘  └───────┬────────┘  └─────────┬─────────┘
     │                 │               │                │                    │
     ▼                 ▼               ▼                ▼                    ▼
┌──────────┐   ┌────────────────┐ ┌──────────────────────────┐   ┌─────────────────────┐
│ Zoekt    │   │ Qdrant Vector  │ │ Memgraph (Bolt)           │   │ Postgres: repo HEAD  │
│ Engine   │   │ DB (versioned  │ │ SCIP + Tree-sitter edges  │   │ SHA + perms table    │
│(Trigram) │   │ collections)   │ │                           │   │ [NEW]                │
└────┬─────┘   └───────┬────────┘ └────────────┬──────────────┘   └──────────┬───────────┘
     │                 │                        │                            │
     └─────────────────┼────────────────────────┴───────────┬────────────────┘
                        ▼                                    ▼
          ┌───────────────────────────────┐   ┌───────────────────────────────┐
          │  cmd/api: Query Classifier     │   │ Permission Cache (short-TTL)  │
          │  [NEW] prunes engine fan-out   │   │ + webhook invalidation [NEW]  │
          └───────────────┬───────────────┘   └───────────────┬───────────────┘
                          │                                    │
                          ▼                                    │
          ┌───────────────────────────────┐                    │
          │ Parallel Go Query Engine       │◄───────────────────┘
          │ (errgroup fan-out, scoped)     │
          └───────────────┬───────────────┘
                          ▼
          ┌───────────────────────────────┐
          │ Merge → Dedup → Rerank (TEI)   │
          │ [UPDATED — concrete design §3A]│
          └───────────────┬───────────────┘
                          ▼
          ┌───────────────────────────────┐
          │  pkg/mcp (go-sdk MCP Server)   │
          └───────────────┬───────────────┘
                          ▼
                 [ Application Teams / AI ]
```

### Go Workspace Package Structure (`cmd/` & `internal/`) **[UPDATED]**

```text
code-kb/
├── cmd/
│   ├── server/            # Main HTTP/gRPC API gateway & MCP server entrypoint
│   └── worker/            # Temporal ingestion & tree-sitter background worker
├── internal/
│   ├── ast/                # Tree-sitter parser, AST visitor, and chunking logic
│   ├── config/             # Viper/envconfig struct loaders
│   ├── domain/             # Core entities (Chunk, Repository, Symbol, UserClaims)
│   ├── diffclassify/        # [NEW] git diff classification: add/mod/delete/rename, force-push detection
│   ├── gateway/             # Keycloak OIDC JWT validator & APISIX middleware
│   ├── graph/               # Memgraph Cypher query builder & Bolt driver wrapper
│   ├── indexer/             # Zoekt trigram index generation pipeline
│   ├── embedding/           # [NEW] gRPC client wrapper for HF TEI embedding inference + collection versioning
│   ├── mcp/                 # MCP tools registration using modelcontextprotocol/go-sdk
│   ├── perms/               # [NEW] permission cache, webhook invalidation handler, Postgres reconciliation poller
│   ├── retrieval/
│   │   ├── classifier.go   # [NEW] query classifier: decides which engines to call
│   │   ├── router.go       # errgroup fan-out across selected engines
│   │   └── merge.go        # [NEW] concrete merge/dedup/rerank pipeline
│   ├── sharding/            # [NEW] rendezvous hashing: repo → gitserver/Zoekt shard assignment
│   └── vector/              # Qdrant gRPC client wrapper & payload filter builders
├── pkg/
│   └── scip/
│       ├── indexers/       # [NEW] registry mapping language → SCIP indexer binary + invocation args
│       └── reader.go        # SCIP index file reader and graph transformer
├── go.mod
└── go.sum
```

---

## 3. Core Go Code Blueprints

### A. Merge → Dedup → Rerank Pipeline **[NEW — replaces the stubbed `mergeAndDeduplicate`]**

The three engines return heterogeneous result types (exact line matches, semantic chunks, graph edges), so they can't be sorted on a single native score. The pipeline normalizes each into a common candidate shape, dedups on identity, then reranks the merged set once — not per-engine.

```go
package retrieval

import (
	"context"
	"sort"

	"code-kb/internal/domain"
)

// CandidateSource records provenance so downstream ranking/telemetry
// can distinguish "found by 2 engines" from "found by 1".
type Candidate struct {
	FilePath   string
	StartLine  int
	EndLine    int
	Content    string
	Sources    []string // e.g. ["zoekt", "qdrant"]
	EngineScore float64 // normalized 0-1 per source engine, kept for debugging
}

func (r *Router) mergeAndRerank(
	ctx context.Context,
	exact []domain.CodeSnippet,
	semantic []domain.CodeSnippet,
	graph []domain.SymbolEdge,
) ([]domain.CodeSnippet, error) {

	// 1. Normalize each engine's output into Candidate, keyed by (file, line-range).
	//    Overlapping ranges from different engines merge into one candidate
	//    with multiple Sources rather than duplicate entries.
	merged := map[string]*Candidate{}
	addOrMerge(merged, exact, "zoekt")
	addOrMerge(merged, semantic, "qdrant")
	addOrMerge(merged, graphToSnippets(graph), "memgraph")

	candidates := make([]*Candidate, 0, len(merged))
	for _, c := range merged {
		candidates = append(candidates, c)
	}

	// 2. Candidates found by multiple engines get a confidence boost
	//    before reranking — cheap signal, applied pre-TEI.
	for _, c := range candidates {
		if len(c.Sources) > 1 {
			c.EngineScore *= 1.15
		}
	}

	// 3. Cap candidate set before the expensive cross-encoder rerank call
	//    (TEI latency scales with input count — this is the primary lever
	//    for staying inside the 800ms budget under load).
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].EngineScore > candidates[j].EngineScore
	})
	if len(candidates) > 50 {
		candidates = candidates[:50]
	}

	// 4. Single rerank call against the merged, deduped, capped set.
	return r.rerankClient.Rerank(ctx, candidates)
}
```

Key design decisions worth calling out explicitly in code review:

* Dedup key is `(file_path, overlapping line range)`, not exact-match, since Zoekt and Qdrant will frequently surface the same function via different offsets.
* The multi-source boost is applied **before** reranking, not as a replacement for it — TEI still makes the final call, this just biases the candidate ordering fed into the cap-at-50 truncation so multi-engine hits aren't dropped before reranking gets to see them.
* Reranking happens **once**, on the merged set — not once per engine (this was ambiguous in the original spec, where reranking was a separate milestone from merging).

### B. Query Classifier **[NEW]**

Runs before the errgroup fan-out to decide which engines are actually worth calling, both for latency and for Qdrant/embedding inference cost.

```go
package retrieval

import "regexp"

var structuralPattern = regexp.MustCompile(`^(func|class|type|struct):`)

type EngineSet struct {
	Zoekt    bool
	Qdrant   bool
	Memgraph bool
}

// Classify is intentionally simple and fast (no LLM call) — it runs
// on every query and must stay well under 1ms.
func Classify(query string, intent QueryIntent) EngineSet {
	switch {
	case intent == IntentGoToDefinition || intent == IntentFindReferences:
		// symbol navigation: graph traversal only
		return EngineSet{Memgraph: true}
	case structuralPattern.MatchString(query):
		// exact symbol/structural query: trigram only, skip embedding cost
		return EngineSet{Zoekt: true}
	case looksLikeRegex(query):
		return EngineSet{Zoekt: true}
	default:
		// natural language query: semantic + graph, skip trigram
		return EngineSet{Qdrant: true, Memgraph: true}
	}
}
```

The `Router.Search` method (from the original spec) is updated to accept an `EngineSet` and only launch `errgroup` goroutines for the engines flagged `true`, rather than always launching all three.

### C. Tree-sitter AST Chunking Engine (`internal/ast/parser.go`)

Extracts function and class blocks using Go Cgo Tree-sitter bindings — unchanged from original spec, no gaps identified here.

```go
package ast

import (
	"context"

	sitter "github.com/smacker/go-tree-sitter"
	"github.com/smacker/go-tree-sitter/golang"
	"code-kb/internal/domain"
)

type ASTChunker struct {
	parser *sitter.Parser
}

func NewGoChunker() *ASTChunker {
	p := sitter.NewParser()
	p.SetLanguage(golang.GetLanguage())
	return &ASTChunker{parser: p}
}

func (c *ASTChunker) ExtractFunctions(ctx context.Context, sourceCode []byte, filePath string) ([]domain.CodeChunk, error) {
	tree, err := c.parser.ParseCtx(ctx, nil, sourceCode)
	if err != nil {
		return nil, err
	}
	defer tree.Close()

	root := tree.RootNode()
	var chunks []domain.CodeChunk

	for i := 0; i < int(root.ChildCount()); i++ {
		child := root.Child(i)
		if child.Type() == "function_declaration" || child.Type() == "method_declaration" {
			nameNode := child.ChildByFieldName("name")
			symbolName := nameNode.Content(sourceCode)

			chunks = append(chunks, domain.CodeChunk{
				FilePath:   filePath,
				SymbolName: symbolName,
				SymbolType: child.Type(),
				Content:    child.Content(sourceCode),
				StartLine:  int(child.StartPoint().Row),
				EndLine:    int(child.EndPoint().Row),
			})
		}
	}
	return chunks, nil
}
```

### D. Diff Classification & Force-Push Detection **[NEW]**

```go
package diffclassify

import (
	"context"
	"errors"
)

type ChangeType string

const (
	Added    ChangeType = "added"
	Modified ChangeType = "modified"
	Deleted  ChangeType = "deleted"
	Renamed  ChangeType = "renamed"
)

type FileChange struct {
	Type     ChangeType
	OldPath  string // set for Renamed/Deleted
	NewPath  string // set for Added/Modified/Renamed
}

var ErrNonLinearHistory = errors.New("parent SHA mismatch: force-push or history rewrite detected")

// ClassifyPush compares the event's parent SHA against the last indexed
// HEAD SHA stored in Postgres. A mismatch means the incoming diff cannot
// be trusted for incremental processing, and the caller must fall back
// to a full repo re-index instead.
func ClassifyPush(ctx context.Context, repoID string, eventParentSHA string, store HeadSHAStore) ([]FileChange, error) {
	lastIndexedSHA, err := store.GetLastIndexedSHA(ctx, repoID)
	if err != nil {
		return nil, err
	}
	if lastIndexedSHA != "" && lastIndexedSHA != eventParentSHA {
		return nil, ErrNonLinearHistory
	}
	// proceed with `git diff --find-renames` classification as normal
	return diffWithRenameDetection(ctx, repoID, eventParentSHA)
}
```

Downstream: `Deleted` changes must fan out to all three stores (Zoekt shard rebuild, Qdrant delete-by-filter, Memgraph node/edge delete) in the same Temporal workflow, not as an afterthought — otherwise ghost results persist after a file is removed.

### E. Repo Sharding **[NEW — rendezvous hashing, ties gitserver/Zoekt scaling into the Go implementation]**

```go
package sharding

import "hash/fnv"

// RendezvousAssign picks the owning shard for a repo using highest-random-weight
// hashing, so adding/removing a shard only moves the repos whose winning
// node changes — not a large fraction of all repos (unlike modulo hashing).
func RendezvousAssign(repoID string, shards []string) string {
	var winner string
	var winnerScore uint64
	for _, shard := range shards {
		h := fnv.New64a()
		h.Write([]byte(repoID + shard))
		score := h.Sum64()
		if score > winnerScore {
			winnerScore = score
			winner = shard
		}
	}
	return winner
}
```

Used independently for gitserver-equivalent shard assignment and Zoekt shard assignment (separate hash rings, since clone load and search-query load scale differently — see architecture discussion). Large monorepos that exceed a single shard's practical capacity get an explicit pinning override in `internal/sharding/overrides.go` rather than relying purely on hash placement.

### F. MCP Server Tool Registration (`internal/mcp/server.go`)

Unchanged from original spec.

```go
package mcp

import (
	"context"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"code-kb/internal/retrieval"
)

type CodeSearchInput struct {
	Query   string   `json:"query" jsonschema:"description=Natural language or symbol query"`
	RepoIDs []string `json:"repo_ids" jsonschema:"description=List of target repository IDs"`
}

func RegisterSearchTool(server *mcp.Server, router *retrieval.Router) {
	mcp.AddTool(server, &mcp.Tool{
		Name:        "codebase_search",
		Description: "Search across centralized code repositories using exact, semantic, and dependency matching.",
	}, func(ctx context.Context, input CodeSearchInput) (*mcp.CallToolResult, any, error) {
		res, err := router.Search(ctx, input.Query, input.RepoIDs)
		if err != nil {
			return mcp.NewToolResultError(err.Error()), nil, nil
		}
		return mcp.NewToolResultText(res.FormatForLLM()), nil, nil
	})
}
```

---

## 4. Persistence & Data Lifecycle **[NEW SECTION]**

| Store | Persistence strategy | Migration/versioning approach |
| --- | --- | --- |
| Memgraph | In-memory by default — **must** enable WAL + periodic snapshotting explicitly, or a restart loses the graph and requires a full SCIP re-import. Size disk for WAL + snapshot headroom, not just RAM for the live graph. | Rebuild-from-SCIP is the disaster-recovery path; snapshots are the fast-restart path. |
| Qdrant | Standard on-disk collections. | Versioned collections (`chunks_v1`, `chunks_v2`, …) per embedding model version. Migration = backfill job into new collection + validation, then flip query-time alias — old collection retained until validated, then dropped. |
| Zoekt shards | Local disk `.zoekt` files, rebuilt from gitserver-equivalent clones. | No versioning needed — rebuilt from source on reindex; replicate to 2 nodes per shard for failover. |
| Postgres | Standard durable store for repo metadata, last-indexed HEAD SHA, permissions cache backing table. | Standard schema migrations (goose/golang-migrate). |

---

## 5. Observability **[UPDATED]**

Beyond the original Prometheus metrics (goroutine counts, search latencies, vector DB connection pools), add:

* **Ingestion lag histogram**: webhook-receipt timestamp → last-store-write-confirmed timestamp, per repo. This is the SLA-facing number — the 15s/commit target in §1 is a proxy, this metric is the ground truth.
* **Permission propagation lag histogram**: access-change webhook receipt → cache invalidation confirmed.
* **Per-engine empty-result rate**: fraction of queries where a given engine (Zoekt/Qdrant/Memgraph) returned zero candidates — feeds back into tuning the query classifier's routing rules in §3.B.
* **Reranker input size distribution**: tracks how often the 50-candidate cap in §3.A is actually binding, to catch cases where truncation is silently dropping relevant results.
* **Force-push/non-linear-history event counter**: how often `ErrNonLinearHistory` fires, since a high rate signals repos where incremental ingestion is providing little value and full-reindex cost should be budgeted for.

---

## 6. Testing & Rollout Strategy **[NEW SECTION]**

* **Golden-query regression set**: a fixed set of representative queries (structural, exact, natural-language, symbol-nav) with expected top-N results, run in CI against any change to `internal/retrieval` (classifier, merge, rerank). Prevents silent ranking-quality regressions as the merge/rerank logic evolves — this was entirely absent from the original phased plan.
* **Shadow indexing for model/chunking changes**: when the embedding model or Tree-sitter chunking logic changes, the new pipeline runs against a shadow Qdrant collection / shard set in parallel with production, validated against the golden-query set before the query-time alias flips. Prevents mid-migration search-quality degradation for live users.
* **Cross-tenant isolation test**: extend the existing security audit (Milestone 3.4) to explicitly include the merge/rerank path — confirm a reranked, multi-engine-sourced result set cannot leak a candidate whose source engine result should have been permission-filtered upstream.

---

## 7. Phased Implementation Plan **[UPDATED — new milestones added, none removed]**

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             GOLANG IMPLEMENTATION                                │
├───────────────────┬───────────────────┬───────────────────┬──────────────────────┤
│ Phase 1 (W1-W4)   │ Phase 2 (W5-W9)   │ Phase 3 (W10-W12) │ Phase 4 (W13-W16)    │
│ Core Services &   │ Multi-Retriever   │ RBAC Gateway &    │ MCP Server &         │
│ Tree-sitter Cgo   │ Go Parallel Engine│ Keycloak Middleware│ Production Scale    │
└───────────────────┴───────────────────┴───────────────────┴──────────────────────┘
```

*(Timeline extended by ~2 weeks vs. the original 14-week plan to accommodate the new milestones below — this reflects real scope, not padding.)*

### Phase 1: Core Services Setup & Ingestion Engine (Weeks 1–4)

* **Milestone 1.1:** Scaffold Go monorepo structure (`cmd/server`, `cmd/worker`, `internal/`) with Go 1.22+ workspace modules.
* **Milestone 1.2:** Implement Temporal worker using `go.temporal.io/sdk` to clone repos and process `git diff` streams asynchronously.
* **[NEW] Milestone 1.2a:** Build `internal/diffclassify` — added/modified/deleted/renamed classification plus parent-SHA force-push detection, backed by the Postgres last-indexed-SHA table.
* **Milestone 1.3:** Build `internal/ast` chunker using `go-tree-sitter` for Go, TypeScript, Python, and Java.
* **Milestone 1.4:** Integrate `github.com/qdrant/go-client` for vector upserts and `github.com/sourcegraph/zoekt` library for trigram indexing.
* **[NEW] Milestone 1.5:** Build `pkg/scip/indexers` registry and subprocess orchestration for `scip-go`, `scip-typescript`, `scip-java`, `scip-python`; define the fallback path (Tree-sitter-only symbol edges) and the "index quality" flag for unsupported languages.
* **[NEW] Milestone 1.6:** Implement delete/rename propagation across Zoekt, Qdrant, and Memgraph within the same Temporal workflow as the triggering diff.

### Phase 2: Parallel Search Router & Reranker Client (Weeks 5–9)

* **Milestone 2.1:** Build `internal/graph` using `neo4j-go-driver` (Bolt-compatible against Memgraph) to parse SCIP symbol index files into Memgraph nodes/edges.
* **[NEW] Milestone 2.1a:** Enable and validate Memgraph WAL + snapshot persistence; document the rebuild-from-SCIP disaster-recovery path.
* **[NEW] Milestone 2.2:** Build `internal/retrieval/classifier.go` — query classification to prune engine fan-out before the errgroup call (structural/exact/nav/semantic routing).
* **Milestone 2.3 (renumbered):** Implement `internal/retrieval/router.go` utilizing `errgroup` for parallel fan-out, now scoped to the classifier's selected `EngineSet`.
* **[NEW] Milestone 2.4:** Implement `internal/retrieval/merge.go` — concrete normalize/dedup/multi-source-boost/cap pipeline (§3.A), replacing the original stub.
* **Milestone 2.5 (renumbered):** Add gRPC client for HuggingFace TEI cross-encoder reranking, wired to run once against the merged/capped candidate set from 2.4.
* **[NEW] Milestone 2.6:** Stand up `internal/embedding` with versioned Qdrant collection support (`chunks_v1`, …) and a backfill migration job skeleton.
* **Milestone 2.7 (renumbered):** Benchmark Go memory allocations (`go test -bench . -benchmem`) and optimize string allocations using `sync.Pool`.
* **[NEW] Milestone 2.8:** Build the golden-query regression test set and wire it into CI for `internal/retrieval`.

### Phase 3: Access Control & Middleware Gateway (Weeks 10–12)

* **Milestone 3.1:** Implement Keycloak OIDC JWT parsing middleware using `github.com/golang-jwt/jwt/v5`.
* **Milestone 3.2:** Build automated Qdrant/Zoekt payload filter injector in Go to scope queries strictly by authorized `repo_ids`.
* **[UPDATED] Milestone 3.3:** Implement `internal/perms` — webhook-driven permission cache invalidation as the primary path (short-TTL cache), with the Postgres sync worker (`jackc/pgx/v5`) demoted to a 15-minute **reconciliation fallback**, not the primary sync mechanism.
* **Milestone 3.4:** Perform security audit ensuring 0% cross-tenant visibility leaks across concurrent goroutine execution paths, **[UPDATED]** explicitly extended to cover the merge/rerank path (§6).

### Phase 4: MCP Server, Sharding, Web UI & Production Deployment (Weeks 13–16)

* **[NEW] Milestone 4.0:** Implement `internal/sharding` — rendezvous hashing for gitserver-equivalent and Zoekt shard assignment, plus the monorepo pinning override table.
* **Milestone 4.1 (renumbered):** Build MCP server endpoint in `cmd/server` using `github.com/modelcontextprotocol/go-sdk` supporting stdio and HTTP/SSE transports.
* **Milestone 4.2:** Compile Go binaries into distroless Docker images and deploy Helm charts to Kubernetes.
* **[UPDATED] Milestone 4.3:** Configure Prometheus metrics covering goroutine counts, search latencies, and vector DB connection pools, plus the new histograms/counters from §5 (ingestion lag, permission propagation lag, per-engine empty-result rate, reranker input-size distribution, force-push event counter).
* **[NEW] Milestone 4.4:** Shadow-indexing rollout path for embedding-model or chunking-logic changes, validated against the golden-query set before alias flip.
* **Milestone 4.5 (renumbered):** Finalize developer documentation and client libraries for internal application teams.
