1. **Null Safety**: ALWAYS prefer `java.util.Optional` (e.g., `Optional.ofNullable(...)`) for handling potential null objects or variables. Do NOT blindly assign default primitive values (like 0 or "") unless contextually required.
2. **Parameter Validation**: If a method parameter is null and shouldn't be, use `java.util.Objects.requireNonNull()` rather than manual if/else blocks.
3. **Fail-Fast**: Never catch NullPointerException. Fix the root cause instead.
4. **Modern Java**: Use Java 21 features where appropriate (Pattern matching, records, enhance switch etc.).
5. **Immutability**: Prefer `final` keywords for variables that should not be reassigned.
6. **Testing**: Don't delete any test cases, try to see what needs a fix — code or test.
7. **Structural Integrity — NEVER VIOLATE**:
   - Do NOT change the package declaration under any circumstances.
   - Do NOT rename any class, interface, enum, or method.
   - Do NOT change a class to an interface, abstract class, enum, or any other type.
   - Do NOT add, remove, or reorder import statements beyond what is strictly required by the fix.
   - Do NOT change method signatures (return type, parameter names, parameter types).
   - Only fix the exact Sonar issues listed. Make the smallest possible change to resolve each issue.