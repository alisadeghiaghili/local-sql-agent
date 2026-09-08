# Local SQL Agent - Security Audit Report

This report documents the findings from a detailed security review of the Local SQL Agent repository, structured for consumption by advanced AI systems like Claude Opus or GPT Astra.

## 1. Overview and Architecture
Local SQL Agent is an NLQ (Natural Language to SQL) application featuring:
* An interactive REPL (`app.py`) and FastAPI service (`api/server.py`).
* An LLM routing engine integrating with OpenAI-compatible APIs (`llm/providers.py`, `llm/sql_agent.py`).
* A robust SQL execution layer running against T-SQL (via `database/executor.py`).
* A strict SQL guard (`security/sql_guard.py`) performing AST-based validation.
* API key authentication and authorization layer (`security/auth.py`).

The architecture demonstrates a strong defense-in-depth approach, explicitly rejecting naive substring matching in favor of robust parsing.

## 2. Configuration and Secrets Management
**Status:** Highly Secure (Defensive Posture)

*   **Secret Storage:** The application strictly avoids hardcoding secrets. It mandates configuration via `.env` / environment variables (`config.py`).
*   **API Key Hashes:** The `API_KEYS_JSON` mechanism stores *only* SHA-256 hashes of API keys, never the raw keys. This limits the blast radius if the configuration file is leaked.
*   **Validation:** Start-up validation (`config.py::Settings.validate`) checks for default placeholders (e.g., `username@server`) to prevent misconfigured instances from starting.
*   **Fail-Closed Authentication:** `AUTH_REQUIRED=true` is the default. If enabled but no keys are configured, the application refuses to start, preventing an open front door.

## 3. Web and API Endpoints
**Status:** Secure

*   **Authentication (Phase 8):** Authentication is handled via Bearer tokens. The implementation uses `hmac.compare_digest` to prevent timing attacks during key resolution (`security/auth.py::resolve_principal`).
*   **Authorization:** Role-based access control (Admin, Operations, Security) is firmly integrated. Access levels are strictly checked via FastAPI dependencies (e.g., `require_admin`).
*   **Rate Limiting:** IP-based and Principal-based rate limiting is applied via `RateLimitMiddleware`. Crucially, authentication failures are bucketed separately to prevent a misbehaving client from starving unauthenticated endpoints like health checks.
*   **Concurrency Limits:** Hard limits on concurrent requests (`ConcurrencyMiddleware`) use a `threading.Lock` to ensure atomic tracking, protecting against DoS by resource exhaustion.

## 4. SQL Execution and Database Security
**Status:** Extremely Robust (Best-in-Class Approach)

*   **AST-based Validation (`security/sql_guard.py`):** The most notable security feature is the complete rejection of substring scanning. Instead, `sqlglot` parses the generated SQL into an AST.
    *   Rejects stacked statements (enforces exactly one statement).
    *   Strictly allows only `SELECT`, `WITH`, `UNION`, `INTERSECT`, `EXCEPT`.
    *   Blocks malicious constructs hiding inside `SELECT` (e.g., `SELECT INTO`, `OPENROWSET`, `xp_cmdshell`).
    *   *All SQL comments are blocked unconditionally* to prevent obfuscation.
*   **Schema Enforcement:** Table references are checked against an allowlist derived from the known schema (`TABLE_COLUMNS`). Hallucinated or unauthorized tables are blocked.
*   **Execution Safety (`database/executor.py`):**
    *   Queries run via raw DBAPI (`exec_driver_sql`), avoiding accidental parameter interpolation by SQLAlchemy.
    *   All queries execute within an explicit transaction that is *always rolled back*. This is a critical safeguard providing read-only behavior even if a modifying statement bypasses the AST guard.
    *   Timeouts (both driver-level and lock timeouts) and hard row limits are enforced to prevent DoS via heavy queries.

## 5. LLM Integrations (MLSecOps)
**Status:** Secure (with inherent LLM limitations)

*   **Prompt Architecture:** The system uses a static prefix + variable suffix architecture. The static prefix (schema, rules) is separated from the user input.
*   **Correction Loop:** LLM errors and guard rejections trigger a self-correction loop. Critically, policy rejections (e.g., trying to access a denied column) immediately terminate the loop. This prevents the LLM from attempting to bypass security controls by rephrasing malicious queries.
*   **Out of Scope Rejection:** The model can explicitly return an `OUT_OF_SCOPE` sentinel, which the application honors and refuses to execute.
*   **Risk:** While prompt injection is always a risk in LLM systems, the strict downstream SQL validation (AST parsing + rollback transactions) neutralizes the primary payload (malicious SQL execution). The LLM acts as an untrusted interpreter, and its output is treated as hostile input.

## 6. Recommendations
The repository exhibits excellent security practices, specifically the shift from substring blocking to AST-based SQL validation.
