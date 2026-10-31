# E2E validation: real Codex tools (only in a disposable workspace)

These are **prompts for Codex App**, not proof that a test already ran. Obtain owner approval to create files and run commands; avoid production projects. Pick a new empty folder (e.g. `~/ioa-codex-test`) as the **Codex App workspace**. Running `cd` in Terminal does *not* change the App's workspace. Use a new session, not a stale `plan`-only session.

## Test 0: Read-only tool execution

Ask: "Actually call `exec_command` to run `pwd; /bin/date -u '+%Y-%m-%dT%H:%M:%SZ'`; show the tool row, exit code and raw stdout. Do not modify files." **Pass** only when an actual `exec_command` row has exit code 0 and matching stdout. Do not trust a plain assistant statement.

## Test A: Expected failing test

Ask Codex:

> Operate **only** in this disposable workspace. First call `exec_command` with `pwd` and stop if it is not the approved workspace. Create `math_ops.py` with `factorial(n)` that raises `ValueError` for negative input, initializes `result=1`, and loops `for i in range(1, n)` (deliberate bug). Create `test_math_ops.py` with unittest assertions `factorial(0)==1`, `factorial(1)==1`, `factorial(5)==120`, and negative input raises. Use actual `apply_patch` and execute `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v` with `exec_command`. **Do not fix the bug yet.** Show actual exit code and stderr/stdout.

**Pass:** 4 tests run, 3 pass, exactly one fails (`AssertionError: 24 != 120`), exit code 1, both files exist in approved workspace. Expected failure here is an intentional success criterion.

## Test B: Repair loop

Ask in the same session:

> Read the two files and the previous test failure. Change **only** `math_ops.py` with `apply_patch` to fix the factorial loop; do not alter the test file. Execute all unittests with `exec_command`. Show actual tool records, exit code and diff. Stop after 3 correction attempts.

**Pass:** fix `range(1, n)` → `range(1, n + 1)`; all 4 tests pass with code 0; test file unchanged.

## Test C: New functionality without regression

Ask in the same session:

> Add `fibonacci(n)` to `math_ops.py`, with fib(0)=0, fib(1)=1, fib(10)=55, negative input raises `ValueError`. Preserve factorial behavior and existing factorial tests. Add four Fibonacci unit tests, run all tests with real tools, reread both files, and report actual results.

**Pass:** 8 tests pass with exit code 0, prior 4 factorial tests unchanged, actual `apply_patch` and `exec_command` records present.

## Longer validation / cost

Only if desired and approved, run 10 further small code changes across the *same disposable project*. Record per-turn success/failure, actual tool names/call IDs, errors, model ID, response `usage` including `input_tokens_details.cached_tokens`, elapsed time, and any billed iOA AI TOKEN. Redact internal request data. Passing three short phases does **not** establish production reliability or provider-native MCP support. An older OpenAI Prompt Cache does not migrate to iOA; backend cache hits must be observed on new requests.
