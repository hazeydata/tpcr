# Tests

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

## X approved-reply poster tests

**File**: `tests/test_x_approved_reply_poster.py` — stdlib unittest with a mocked X client (no network, no secrets).
Run: `unshare -rn python3 tests/test_x_approved_reply_poster.py`. Covers hash binding, validation, mention pre-check,
dry run, duplicate/crash recovery, 500/429/media failures, the 5-per-run cap and 30 s spacing, non-object approvals (AC 2),
403 scope (POST only -> manual), fsync per ledger line (patched `os.fsync`), queue-dir permission refusal (AC 3a) and the
ledger's queue commit SHA (AC 3b, local `git init` in a temp dir, including a recovered post), refusal before the ledger is
even created (no empty ledger file), and signed approvals (R5 / AC 3c): unsigned, garbage, tampered payload (text with recomputed
hashes, target, entry_id, quote, approved_at, alt text, image bytes), tampered signature and other-key signatures are
`refused_unsigned` with 0 calls; a good signature posts on the mock; `main()` exits 1; the poster refuses a missing, private,
non-Ed25519 or in-queue verify key; and the signing helper `x_reply_sign_approval.py` (keygen 0600, no overwrite, refuses loose
mode / in-queue / in-git keys, refuses to sign a hash mismatch). Keys are generated per test in a temp dir. 72 tests. Test fixtures use synthetic handles and post ids only.
