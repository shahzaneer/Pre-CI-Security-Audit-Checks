# CI Security Checks

A reusable, polyglot **Pre-CI Security Audit** pipeline that acts as a gating mechanism before deployment. It performs four parallel security scans — supply-chain malware detection, static analysis (SAST), software composition analysis (SCA), and package maturity verification — then aggregates results into a single pass/fail dashboard.

Supports both **JavaScript / Node.js frontend** and **Python backend** projects via language-specific GitHub Actions reusable workflows.

---

## Architecture

```
                            workflow_call / push trigger
                                       │
         ┌─────────────────┬───────────┼───────────┬─────────────────┐
         ▼                 ▼           ▼           ▼                 ▼
    ┌─────────┐      ┌─────────┐  ┌─────────┐  ┌─────────┐    ┌──────────┐
    │  depx   │      │  sast   │  │   sca   │  │   age   │    │  (repo)  │
    │ malware │      │ semgrep │  │  trivy  │  │package  │    │ inventory│
    │  scan   │      │  scan   │  │  scan   │  │maturity │    │  (lock)  │
    └────┬────┘      └────┬────┘  └────┬────┘  └────┬────┘    └────┬─────┘
         │                │            │            │              │
         └────────────────┼────────────┼────────────┘──────────────┘
                          │            │
                          ▼            ▼
                    ┌──────────────────────┐
                    │       gate job       │
                    │  aggregate + decide   │
                    └──────────┬───────────┘
                               │
                     outputs:  has_failures  (boolean)
                               failure_count (integer)
```

All four scan jobs run in **parallel**. The **gate job** waits for all of them (`needs: [depx, sast, sca, age]`), downloads their Markdown artifact sections, concatenates them into a single dashboard in `$GITHUB_STEP_SUMMARY`, evaluates pass/fail, and surfaces the results as workflow outputs that downstream jobs can branch on.

---

## Directory Layout

```
ci-checks/
├── .vscode/
│   └── settings.json                     # VS Code Snyk integration
├── javascript-frontend/
│   ├── security_audit.yaml               # Reusable workflow for Node.js / npm projects
│   └── verify_package_age.cjs            # Standalone npm package-age verifier (Node.js)
└── python-backend/
    ├── security_audit.yaml               # Reusable workflow for Python / PyPI projects
    └── verify_package_age.py             # Standalone PyPI package-age + hash verifier (Python)
```

---

## On False Positives

No security scanner achieves 100% accuracy. The tools in this pipeline are the **best-in-class free and open-source options** in their respective categories (depx, Semgrep, Trivy) — widely adopted, actively maintained, and offering the highest signal-to-noise ratio without requiring a paid license.

To keep false positives from blocking development, only the most confident signals gate deployment. Lower-severity or older findings appear as dashboard warnings — visible and actionable, but non-blocking.

---

## The Four Gates — Why Each Step Matters

### 1. Supply-Chain Malware Detection (`depx`)

**Tool**: [depx](https://github.com/projectdiscovery/depx) by ProjectDiscovery

**What it does**: Extracts all dependencies declared in `package.json` and `requirements.txt`, pipes them to `depx`, which cross-references each package against a database of known malicious packages. Matches are classified as **recent** (published ≤ 30 days ago) or **known** (older than 30 days).

| | |
|---|---|
| **Data source** | ProjectDiscovery curated database (community reports, security research, registry analysis) |

- Recent malicious packages **block** the pipeline.
- Known malicious packages surface as **informational warnings** (collapsed in a `<details>` block).

**Why it's needed**: Package registries are the #1 vector for supply-chain attacks. Attackers publish typo-squatted or dependency-confusion packages that exfiltrate secrets, mine cryptocurrency, or inject backdoors. `depx` catches these before they ever execute in your CI environment — blocking the attack at the earliest possible point.

**Failing condition**: Any dependency flagged as a **recent** malicious package (`depx_recent > 0`).

---

### 2. Static Application Security Testing — SAST (`sast`)

**Tool**: [Semgrep](https://semgrep.dev) (OSS engine)

**What it does**: Runs `semgrep scan --config auto` against the entire source tree (excluding `.github/` and Kubernetes manifests). It detects patterns known to cause vulnerabilities — hardcoded secrets, SQL injection, path traversal, insecure deserialization, and more. Findings are bucketed by severity:

| | |
|---|---|
| **Data source** | Semgrep community rule registry (OWASP Top 10, CWE Top 25, community contributions) |

| Severity | Impact |
|----------|--------|
| `ERROR` | **Blocks** the pipeline |
| `WARNING` | Informational (dashboard only) |
| `INFO` | Informational (dashboard only) |

**Why it's needed**: SAST shifts security left by catching vulnerabilities during development, *before* code reaches production. Unlike linters, Semgrep understands security semantics — it won't just flag "bad style," it will flag "this code pattern leads to RCE." Running it as a CI gate prevents known-dangerous patterns from ever being deployed.

**Failing condition**: One or more `ERROR`-severity Semgrep findings (`sast_errors > 0`).

---

### 3. Software Composition Analysis — SCA (`sca`)

**Tool**: [Trivy](https://github.com/aquasecurity/trivy) v0.71.2 by Aqua Security

**What it does**: Scans all third-party dependencies in the repository (lockfiles, vendored packages, site-packages) against vulnerability databases. Identifies packages with known CVEs and scores by severity:

| | |
|---|---|
| **Data sources** | NVD, GHSA, GitLab Advisories, Red Hat, Debian, npm, PyPA, RubySec |

| Severity | Impact |
|----------|--------|
| `CRITICAL` / `HIGH` | **Blocks** the pipeline |
| `MEDIUM` / `LOW` | Informational (dashboard only) |

**Why it's needed**: Even if your own code is secure, your dependencies may not be. Log4Shell (CVE-2021-44228), the `event-stream` incident, and the `xz` backdoor all demonstrate that vulnerabilities in transitive dependencies can be catastrophic. SCA answers: *"Are we shipping known-vulnerable code?"*

**Failing condition**: Any dependency with a `CRITICAL` or `HIGH` CVE (`sca_critical > 0`).

---

### 4. Package Maturity Verification (`age`)

**Tools**: Custom scripts — `verify_package_age.cjs` (Node.js) and `verify_package_age.py` (Python), plus inline Python in the workflows.

| | |
|---|---|
| **Data sources** | npm registry (`registry.npmjs.org`) / PyPI JSON API (`pypi.org/pypi`) — authoritative live registry metadata |

**What it does**: Queries the npm registry or PyPI for every pinned dependency and checks its **publishing date**. Any package published fewer than 30 days ago is flagged.

#### JavaScript (`verify_package_age.cjs`)
- Parses `package-lock.json` (supports v1, v2, and v3 lockfile formats)
- Queries `https://registry.npmjs.org/<name>` with proper URL encoding for scoped packages (`@types%2Fnode`)
- Uses a custom `asyncPool` helper to limit concurrency to 25 simultaneous HTTP requests
- Handles rate limiting (HTTP 429) with exponential backoff: [1s, 3s, 5s, 10s, 15s]
- Outputs `.pkg-age-report.json` (structured JSON) and `.pkg-age-errors.json` (for Slack/webhook integration)
- In the workflow, the script is called at `node .github/workflows/verify_package_age.cjs` — meaning this file is expected to be copied into the target repo's `.github/workflows/` directory

#### Python (inline in YAML + standalone `verify_package_age.py`)
- **Workflow (inline)**: The Python `security_audit.yaml` performs age checking entirely inline — it reads `requirements.txt`, queries PyPI's JSON API via `urllib` with a `ThreadPoolExecutor(max_workers=16)`, and generates the Markdown section directly. No external file call needed.
- **Standalone script** (`verify_package_age.py`): A richer implementation that additionally:
  - Parses `--hash` lines from `requirements.txt`
  - **Verifies hashes against PyPI's published digests** to detect tampering or mirror compromise
  - Supports line continuations (`\`), environment markers, extras notation (`psycopg[binary]`)
  - Outputs `package_age_report.json` and a styled `package_age_report.html`

**Why it's needed**: Brand-new packages have far less community scrutiny. Attackers exploit this by publishing malicious packages hoping to compromise projects before the packages are reported and removed. A 30-day minimum age enforces a "cooling-off" period — if a package is a supply-chain attack, there's a much higher chance it will have been discovered and taken down within 30 days. The hash verification (Python standalone) adds a second layer: verifying a package's hash matches the registry's official digest guards against mirror compromise and man-in-the-middle substitution.

**Failing condition**: Any package published fewer than 30 days ago (`age_failures > 0`).

> **`force_deploy` override**: The `age` check can be bypassed via the `force_deploy` workflow input (e.g., for emergency hotfixes that need a genuinely new package). All three other checks remain enforced regardless.

---

## How the Gate Aggregates Results

The `gate` job runs after all four parallel jobs complete. It:

1. Downloads all four `*-section` artifacts (Markdown fragments from depx, sast, sca, age).
2. Concatenates them into `$GITHUB_STEP_SUMMARY`, producing a single unified dashboard.
3. Reads the numeric outputs from each job: `depx_recent`, `sast_errors`, `sca_critical`, `age_failures`.
4. Sums them into a total `failure_count`.
5. If `force_deploy` is `true`, subtracts `age_failures` from the total but still warns.
6. Sets `has_failures` (boolean) and `failure_count` (integer) as workflow outputs.

A downstream deploy job can then gate on:
```yaml
if: needs.security.outputs.has_failures == 'false'
```

---

## Workflow Differences: JavaScript vs Python

| Aspect | JavaScript | Python |
|--------|-----------|--------|
| Push trigger branch | `stager` | `security-audit-depx` |
| `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` | Set on all 5 jobs | Not set |
| Semgrep `setup-python` cache | `~/.cache/pip` only | `cache: 'pip'` + `~/.cache/pip` |
| Age check implementation | Calls external `node .github/workflows/verify_package_age.cjs`, then inline Python renders the Markdown | Fully inline Python — no external script |
| Age concurrency | 25 requests (Node.js `asyncPool`) | 16 workers (Python `ThreadPoolExecutor`) |
| `download-artifact` action | `@v7` | `@v8` |
| Age script outputs | `.pkg-age-report.json`, `.pkg-age-errors.json` | `age_section.md` directly |

Despite these differences, both workflows produce identical outputs (`has_failures`, `failure_count`) and follow the same four-gate architecture.

---

## How to Use

### Calling from Another Workflow (Recommended)

```yaml
# In your downstream repo's .github/workflows/deploy.yaml
jobs:
  security:
    name: Pre-CI Security Audit
    uses: your-org/ci-checks/.github/workflows/javascript-frontend/security_audit.yaml@main
    with:
      repo_type: javascript     # 'python', 'javascript', or 'both'
      force_deploy: false       # set to 'true' to bypass age check only

  deploy:
    needs: security
    if: needs.security.outputs.has_failures == 'false'
    runs-on: ubuntu-latest
    steps:
      - run: echo "All security checks passed — deploying!"
```

---

## Workflow Outputs

| Output | Type | Description |
|--------|------|-------------|
| `has_failures` | `boolean` | `true` if any security gate failed (excluding force-deployed age checks) |
| `failure_count` | `integer` | Total number of failing items across all four checks |

---

## Tooling & Dependencies

| Layer | Tool | Version | Purpose |
|-------|------|---------|---------|
| CI Runtime | GitHub Actions | — | Orchestration |
| Supply-chain | [depx](https://github.com/projectdiscovery/depx) | latest | Malicious package detection |
| SAST | [Semgrep](https://semgrep.dev) | latest (pip) | Static vulnerability scanning |
| SCA | [Trivy](https://github.com/aquasecurity/trivy) | 0.71.2 | Known CVE detection |
| Age — JS | Node.js | ≥ 22 | npm registry queries |
| Age — Python | Python | ≥ 3.12 | PyPI registry queries |
| Caching | `actions/cache` | v6 | depx vendor database, pip cache, Trivy vulnerability DB |
| Artifacts | `actions/upload-artifact@v7` / `actions/download-artifact@v7,v8` | v7 / v8 | Cross-job Markdown + JSON data sharing |
| IDE | VS Code + Snyk extension | — | Local developer-side scanning |

---

## Benefits

- **Shift-left security**: Catch vulnerabilities before they reach production — not after.
- **Defense in depth**: Four independent, non-overlapping security perspectives. Supply-chain malware, SAST, SCA, and package maturity each catch different classes of risk.
- **Best-in-class free tooling**: Every tool in the pipeline is open-source, free, and widely adopted — no vendor lock-in, no paid licenses required, yet each represents the gold standard in its category (depx for malware detection, Semgrep for SAST, Trivy for SCA).
- **False-positive aware design**: The pipeline is built with the understanding that no scanner is perfect. Only the highest-confidence signals (recent malware, ERROR-level SAST, CRITICAL/HIGH CVEs, sub-30-day packages) block deployment. Lower-severity findings appear as informational dashboard warnings — visible and actionable without halting releases.
- **Parallel execution**: All four scans run concurrently, so total gate latency equals the duration of the *slowest* scan, not the sum of all four.
- **Reusable by design**: A single workflow file is callable from any number of downstream repositories — no copy-paste drift.
- **Emergency override**: The `force_deploy` flag lets you ship a hotfix that depends on a genuinely new package without disabling the other three security gates.
- **Rich, unified reporting**: Every run produces a single human-readable Markdown dashboard in the GitHub Actions summary UI, with file paths, line numbers, CVE IDs, and package versions — so developers can triage findings at a glance without hunting through logs.
- **Standalone verification**: The age-check scripts can be run locally or in any CI system, not just GitHub Actions.
- **Language parity**: JavaScript and Python projects get equivalent protection through a shared architecture with minimal, well-documented language-specific divergence.
