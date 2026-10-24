# Tests

## Live Waits Lock Tests (TPCR-S11-LOCK-001)

**File**: `tests/test_live_waits_lock.py`

Covers the scraper's `live_waits` write retry (~180s budget), the pending-batch dir
(`state/live_waits_pending/`), the daily staging replay (`src/replay_live_waits.py`),
row prep (6 AM park-date rule; rows dropped for null/empty keys are counted and logged as a WARNING) and `replay_due`. The s11 write lock is simulated with a
child process holding the DuckDB file open. Uses `tmp_path` only; takes ~15s.

```bash
python -m pytest tests/test_live_waits_lock.py
```

## Entity Metadata Index Tests

**File**: `tests/test_entity_index.py`

Comprehensive tests for the Entity Metadata Index system.

### Running Tests

```bash
# Run all tests
python tests/test_entity_index.py

# Run with verbose output
python tests/test_entity_index.py --verbose
```

### Test Coverage

1. **Index Creation**: Verifies database schema is created correctly
2. **Index Update**: Tests updating index from DataFrame
3. **Incremental Update**: Tests that existing entities are updated correctly
4. **Query Entities Needing Modeling**: Tests finding entities with new observations
5. **Load Entity Data**: Tests selective CSV reading (only relevant park's CSVs)
6. **Mark Entity Modeled**: Tests marking entities as modeled
7. **Min Age Hours Filter**: Tests filtering by observation age

### Test Environment

Tests create temporary files in `temp/test_entity_index/` (cleaned up after). They:
- Create sample fact CSVs
- Build/update entity index
- Verify all operations work correctly

### Note on Sandbox Restrictions

If you see "disk I/O error" when running in a sandboxed environment, this is expected. SQLite needs to write database and journal files, which may be blocked by sandbox restrictions. The tests will work correctly when run outside the sandbox (e.g., in your local environment or CI/CD).

### Expected Output

```
======================================================================
Entity Metadata Index Tests
======================================================================
Temp directory: D:\...\temp\test_entity_index

PASS: Index Creation
PASS: Index Update
PASS: Incremental Update
PASS: Query Entities Needing Modeling
PASS: Load Entity Data
PASS: Mark Entity Modeled
PASS: Min Age Hours Filter

======================================================================
Results: 7 passed, 0 failed
======================================================================
```

## Live waits backfill tests

**File**: `tests/test_backfill_live_waits_oct1_9.py` — `scripts/backfill_live_waits_oct1_9.py` (TPCR-BACKFILL-OCT1-9): source-parity gate (row count, entity set, type mix, TouringPlans POSTED), dry run, idempotent load, path-safety refusals. Uses `tmp_path` only; run with `pytest tests/test_backfill_live_waits_oct1_9.py`.
