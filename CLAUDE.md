# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Commands

### Development Setup
```powershell
# 1. Install dependencies (already done in .venv)
.venv\Scripts\python.exe -m pip list  # Verify installed packages

# 2. Configure environment (.env is gitignored - never commit it)
# Copy .env.example to .env and fill in required values:
# - GOOGLE_VISION_API_KEY (H1) - for live web search
# - PINATA_JWT (H2) - for IPFS pinning  
# - AMOY_PRIVATE_KEY (H3) - funded Amoy testnet wallet (throwaway!)
# - SERPAPI_KEY (optional fallback)

# 3. Pre-download ML model (run once)
.venv\Scripts\python.exe scripts\bootstrap_model.py

# 4. Start the API server
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### Testing
```powershell
# Run all tests (90+ tests covering happy/unhappy paths)
.venv\Scripts\python.exe -m pytest -q

# Run specific test files
.venv\Scripts\python.exe -m pytest tests/test_backend.py -q
.venv\Scripts\python.exe -m pytest tests/test_search_extensions.py -q
.venv\Scripts\python.exe -m pytest tests/test_gallery.py -q

# Run with coverage
.venv\Scripts\python.exe -m pytest --cov=app --cov=services --cov-report=term-missing
```

### Live Validation (requires funded wallet)
```powershell
# Check environment variables format
.venv\Scripts\python.exe scripts\go_live.py env

# Test Amoy connectivity and wallet balance
.venv\Scripts\python.exe scripts\go_live.py amoy

# Test Pinata IPFS pinning
.venv\Scripts\python.exe scripts\go_live.py pinata

# Test Google Vision API (requires billing enabled)
.venv\Scripts\python.exe scripts\go_live.py vision

# Run full end-to-end pipeline (real face -> search -> verification -> anchor)
.venv\Scripts\python.exe scripts\go_live.py e2e

# Run all validation steps in sequence
.venv\Scripts\python.exe scripts\go_live.py all
```

### Contract Deployment
```powershell
# Deploy AnchorRecord contract once (writes address to .env)
.venv\Scripts\python.exe scripts\deploy_contract.py
```

## Architecture Overview

### Core Pipeline Flow
```
image → VisionService (detect face, 512-d embedding)
      → SearchService (retrieval ONLY: candidates, no scoring)
      → VerificationService (independent re-fetch / re-detect / re-encode / re-score)
      → BlockchainService (canonical record → SHA-256 → Pinata IPFS → Amoy anchor)
      → re-verification (rebuild hash vs. chain for tamper-evidence demo)
```

### Key Services
1. **VisionService** (`services/vision.py`)
   - Face detection + embedding using InsightFace buffalo_l (CPU-only)
   - Returns query embedding held by backend for verification
   - Never sends embedding to SearchService (contract enforcement)

2. **SearchService** (`services/search.py`)
   - Reverse-image retrieval ONLY (no scoring/judgment)
   - Primary: Google Cloud Vision Web Detection
   - Fallback: SerpAPI (when configured)
   - Extensions: DuckDuckGo scraping, Enrolled Gallery, Federated search
   - Strict zero-scoring contract: `extra="forbid"` on SearchOutput

3. **VerificationService** (`services/verification.py`)
   - Independent from-scratch verification
   - Fresh model call, independent fetch path, independent scoring
   - Thresholds: ACCEPT_THRESHOLD=0.48 (HIGH), REVIEW_THRESHOLD=0.35 (UNCERTAIN)
   - Never accepts pre-computed similarity from SearchService

4. **BlockchainService** (`services/blockchain.py`)
   - Builds canonical record (no biometric data)
   - IPFS pinning via Pinata (best-effort, nullable content_cid)
   - Polygon Amoy anchoring (chainId 80002) with retry/backoff
   - Tamper-evidence re-verification

5. **Gallery Service** (`services/gallery.py`)
   - Enrolled Identity Directory for common persons (students, staff)
   - Zero-trust candidate generation for search
   - Canonical profile HTML with OpenGraph tags

6. **Backend API** (`app/main.py`)
   - FastAPI job manager with background pipeline execution
   - SSE event streaming for live data-lineage feed
   - Bounded job registry (200 jobs, 2h TTL)
   - Enrolled gallery endpoints

### Critical Contracts (CONTRACTS.md)
- **§1 VisionService**: Embedding is query embedding held by backend, never sent to Search
- **§2 SearchService**: Strict retrieval only - forbids embedding/similarity/confidence/match_decision fields
- **§3 VerificationService**: Receives candidate coords + query embedding (held separately by backend)
- **§4 Canonical Record**: Minimized JSON - content_hash, content_cid, source_reference_hash=sha256(candidate_url)
- **§5 Event Log**: Data lineage with stages: face_detected → query_sent → candidates_returned → candidate_selected → verification_run → verification_result → record_built → blockchain_tx_submitted → blockchain_confirmed → reverification_run
- **§6 API Surface**: POST /api/pipeline/start → {job_id}, GET /api/pipeline/{job_id}/result → full result + event log

### Key Design Principles
1. **Zero-Trust Independence**: Search never scores; verification does independent re-detection/scoring
2. **Honest Failure Modes**: Every failure is a typed terminal state (NO_FACE_DETECTED, LOW_IMAGE_QUALITY, NO_SEARCH_RESULTS, SEARCH_API_FAILURE, VERIFICATION_FAILED, BLOCKCHAIN_FAILURE)
3. **Data Lineage Integrity**: No scoring data leaks into event logs or search outputs
4. **Immutable Patterns**: Prefer new objects over mutation (per ECC coding standards)
5. **Contract-First Development**: All services validate against pydantic models with `extra="forbid"`

## Development Workflow
Follow the mandatory ECC development workflow:
1. **Research & Reuse**: GitHub search → Library docs (Context7) → Check for adaptable implementations
2. **Plan First**: Use planner agent for implementation planning
3. **TDD Approach**: Write tests first (RED) → Implement to pass (GREEN) → Refactor (IMPROVE) → Verify 80%+ coverage
4. **Code Review**: Use code-reviewer agent immediately after writing code
5. **Commit & Push**: Detailed commit messages following conventional commits

## Important Files to Review
- `README.md` - Complete system overview and run instructions
- `CONTRACTS.md` - Single source of truth for all service interfaces
- `TASK3_ARCHITECTURE.md` - Detailed architecture diagrams
- `HUMAN_ACTIONS.md` - Required manual steps (wallet funding, billing enablement)
- `PROJECT_STATUS.md` - Current live validation status
- `MULTI_AGENT_BUILD_PLAN.md` - Build orchestration details
- `app/main.py` - Backend pipeline orchestration
- `services/` directory - All service implementations
- `tests/` directory - Comprehensive test suite (90+ tests)

## Code Quality Standards
- Functions < 50 lines, files < 800 lines
- No deep nesting (>4 levels) - use early returns
- Explicit error handling at every level
- Input validation at system boundaries
- Immutability preferred (create new objects, don't mutate existing)
- No hardcoded secrets - use environment variables
- Type annotations on all function signatures (Python)
- Comprehensive test coverage (80%+ minimum)
- No console.log/debug statements in production code

## Remember
- `.env` file is gitignored and must never be committed
- Amoy wallet must be a throwaway testnet wallet ONLY (never mainnet)
- Never print, log, or expose private key values
- Raw biometric vectors or unhashed URLs are never stored on-chain
- All secrets must come from environment variables via os.getenv