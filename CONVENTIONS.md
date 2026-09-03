# ReconAgent — Conventions

> These rules apply to every file in the repo. They exist because two audiences
> will read this code: hackathon judges evaluating correctness, and a hiring manager
> evaluating production-readiness. Cut no corners here.

---

## Python

### Type Hints

**Required on every function signature** — parameters and return type.

```python
# ✅ correct
def compute_match_score(order: NormalisedOrder, settlement: NormalisedSettlement) -> MatchScore:
    ...

# ❌ wrong — no hints
def compute_match_score(order, settlement):
    ...
```

- Use `from __future__ import annotations` at the top of every module to enable
  PEP 563 postponed evaluation (avoids forward-reference issues).
- Use `Optional[X]` / `X | None` for nullable types. Prefer `X | None` (Python 3.10+).
- Use `TypeAlias` for complex repeated types.

### Docstrings

**Required on every public function, class, and module.**
Use Google-style docstrings:

```python
def normalise_utr(raw: str) -> str:
    """Canonicalise a UTR string to a consistent format for matching.

    Strips hyphens, normalises to uppercase, and removes common prefix
    variations so that "UTR-2024-SBIN-Q4FM..." and "utr2024sbinq4fm..."
    resolve to the same key.

    Args:
        raw: The raw UTR string as it appears in the source document.

    Returns:
        A canonical UTR string suitable for exact-match comparison.

    Raises:
        ValueError: If `raw` is empty after stripping whitespace.
    """
```

Private functions (prefixed `_`) should have at least a one-line comment explaining
their purpose.

### Formatting

- **Formatter:** `black` (line length 88)
- **Linter:** `ruff` (replaces flake8 + isort)
- Run both before committing:
  ```bash
  black backend/
  ruff check backend/ --fix
  ```
- Configuration lives in `pyproject.toml` (added in Chunk 2).

### Imports

Ordered by `ruff`/`isort`:
1. Standard library
2. Third-party
3. Internal (`from app.models import ...`)

Always use absolute imports within the `app` package.

### Amounts / Money

**Never use `float` for monetary comparisons.**
Use `decimal.Decimal` with explicit precision:

```python
from decimal import Decimal, ROUND_HALF_UP

amount = Decimal("17252.99")
fee    = Decimal("353.40")
net    = (amount - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```

This is enforced because a ₹0.01 rounding difference is a meaningful reconciliation
signal; float drift creates false positives.

---

## Testing

### Framework

**pytest** — no unittest, no nose.

```bash
pytest backend/ -v
```

### Location

Tests live **alongside the module they test** — not in a separate top-level `tests/`
directory:

```
backend/app/services/
├── matching.py
├── test_matching.py      ← tests for matching.py
├── classification.py
└── test_classification.py
```

### Coverage requirement

**Every new service module ships with at least one test file before it is considered
done.** A module without tests does not get its TASKS.md checkbox ticked.

### Test naming

```python
def test_<what>_<condition>_<expected_outcome>():
    ...

# examples:
def test_normalise_utr_hyphenated_returns_canonical():
def test_match_score_exact_utr_returns_max_weight():
def test_classify_no_settlement_returns_exception_failed_payment():
```

### Fixtures

Common fixtures (sample CSV rows, normalised transaction objects) live in
`backend/conftest.py`.

### What to test

- **Unit tests:** Each matching signal function in isolation.
- **Integration tests:** Full pipeline on the synthetic dataset — assert that ground
  truth labels match classified outputs (precision/recall check).
- **Edge tests (Chunk 9):** Duplicate UTR, missing amounts, non-INR currencies,
  empty CSVs, malformed dates.

---

## Secrets & Configuration

**No hardcoded secrets or API keys anywhere in the codebase — ever.**

```python
# ✅ correct
import os
api_key = os.environ["GEMINI_API_KEY"]   # or via pydantic-settings

# ❌ wrong — never do this
api_key = "AIzaSy..."
```

- All config loaded via `pydantic-settings` `BaseSettings` class (Chunk 2).
- `.env` is gitignored. `.env.example` is committed with empty values and comments.
- Validate at startup — if a required env var is missing, the app fails fast with a
  clear error message rather than failing later at the call site.

---

## Commit Messages

Use **Conventional Commits** format:

```
<type>(<scope>): <short description>

[optional body]

[optional footer]
```

**Types:**

| Type | When to use |
|---|---|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code restructuring with no behaviour change |
| `test` | Adding or updating tests |
| `docs` | Documentation only |
| `chore` | Build scripts, deps, CI config |
| `perf` | Performance improvement |

**Examples:**

```
feat(matching): implement UTR canonicalisation and exact-match signal
fix(classification): handle edge case where settlement amount is zero
test(matching): add integration test against synthetic dataset ground truth
docs(architecture): update DDL log with SQLite decision rationale
refactor(normalisation): extract amount-coercion to shared utility
```

- Keep the subject line ≤ 72 characters.
- Use the body to explain *why*, not *what* (the diff shows what).
- Reference chunk number in the body for traceability: `Part of Chunk 3 — matching engine`.

---

## Module Structure Template

Every new service module should follow this template:

```python
"""
<module_name>.py
────────────────
One-sentence summary of what this module does.

⚠️  [Include LLM prohibition note if this module touches matching logic]
"""

from __future__ import annotations

# stdlib
import ...

# third-party
import ...

# internal
from app.models import ...

# ── Constants ──────────────────────────────────────────────────────────────
...

# ── Public API ─────────────────────────────────────────────────────────────
def public_function(...) -> ...:
    """Docstring."""
    ...

# ── Internal helpers ───────────────────────────────────────────────────────
def _private_helper(...) -> ...:
    # one-line comment
    ...
```

---

## Definition of Done (per chunk)

A chunk is considered done when:

1. ☑ All planned files are created
2. ☑ Type hints present on all public functions
3. ☑ Docstrings on all public functions
4. ☑ At least one test file exists per new service module
5. ☑ `black` and `ruff` pass with zero errors
6. ☑ TASKS.md checkbox is ticked with a one-line completion note
7. ☑ ARCHITECTURE.md status updated for affected components
