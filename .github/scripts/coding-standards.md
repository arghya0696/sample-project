1. **None Safety**: Always check for `None` before accessing attributes or calling methods. Prefer early returns or `if x is None` guards. Use `Optional[T]` type hints to make nullable values explicit.
2. **Parameter Validation**: Raise `ValueError` or `TypeError` with a clear message when inputs are invalid. Do this at function boundaries — not deep in internal logic.
3. **Fail-Fast**: Never catch `AttributeError` or `TypeError` caused by a `None` value — fix the root cause instead of masking it.
4. **Modern Python**: Use Python 3.11+ features where appropriate (match/case, type hints, dataclasses, `tomllib`, exception groups, etc.).
5. **Immutability**: Prefer tuples over lists and `frozenset` over `set` for data that should not change after creation.
6. **Testing**: Don't delete any test cases. If a test is failing, fix the source code or correct the assertion — never skip or comment out the test.
7. **Structural Integrity — NEVER VIOLATE**:
   - Do NOT change the module name or package structure.
   - Do NOT rename any class, function, or method.
   - Do NOT change function signatures (return type, parameter names, parameter types).
   - Do NOT add, remove, or reorder imports beyond what is strictly required by the fix.
   - Only fix the exact issues identified. Make the smallest possible change to resolve each issue.
