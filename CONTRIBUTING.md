# Contributing

Short version: **keep the invariants true, keep the hot paths logarithmic, and let the
tests argue for you.**

## Setup

```bash
pip install -e ".[dev]"
make test-fast     # unit + property + stateful (~5s)
make check         # lint + types + full test suite
```

## Ground rules

1. **Every status change goes through `SeatAllocationEngine._transition`.** It is the single
   choke point that keeps the running counters (`held_seats()`, status tallies) accurate, and
   therefore the thing that keeps the hot paths `O(log n)` instead of accidentally `O(n)`.
2. **Never delete a registration.** Move it to a terminal status. History is what makes the
   audit export and `replay()` possible.
3. **New behaviour needs a test named after the promise it protects** (see `test_engine.py`),
   and preferably a property test if it involves ordering or capacity.
4. **If you touch a hot path, run the empirical tests** (`pytest tests/test_complexity.py`).
   They assert growth ratios, not milliseconds, so a passing run on a slow machine is
   meaningful.
5. **Determinism is a feature.** Pass `at=` explicitly rather than reading the clock; a change
   that makes output depend on wall-clock time is a regression.

## Adding an operation (checklist)

- [ ] Add the transition in `engine.py` and emit an audit event for it.
- [ ] If it mutates state, add the op name to `PRIMARY_OPS` in `persistence.py` and teach
      `replay()` how to re-apply it.
- [ ] Add an example-based test, plus an invariant/stateful rule if it can break one.
- [ ] Update the complexity table in the README and `docs/COMPLEXITY.md`.
- [ ] Run `make bench` if the change affects a hot path (paste the new table into the README).

## Reporting a bug

Repo: <https://github.com/4yushman/seatalloc> — open an issue at
<https://github.com/4yushman/seatalloc/issues>.

Include the audit log (`.jsonl`) and the output of `python -m seatalloc verify <log>`. If the
log reproduces it, the bug is reproducible by definition — that is the whole point of the
append-only design.
