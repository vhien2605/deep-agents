---
name: maven-junit
description: How to build a Java project and write/run JUnit tests with Maven - runner detection, JUnit 5 conventions, running the suite, and interpreting common failures.
---

# Building and testing Java with Maven + JUnit

## Detecting how to run the project

- `pom.xml` present → Maven. Use the wrapper if it exists:
  `./mvnw` (check with glob) else `mvn`. Run via run_command, e.g.
  `run_command("mvn -q test")`.
- `build.gradle`/`build.gradle.kts` → Gradle project; the same JUnit
  conventions apply but commands are `./gradlew test` and JaCoCo config
  differs — report this and adapt.
- Read `pom.xml` first: JUnit 4 (`junit:junit`) vs JUnit 5
  (`org.junit.jupiter:junit-jupiter`), Java version
  (`maven.compiler.source`/`release`), modules (`<modules>` = multi-module).

## Test conventions (JUnit 5 preferred)

- Files: `src/test/java/<same/package>/<ClassName>Test.java` — mirror the
  production package so package-private members are visible.
- Style: `@Test` methods, `Assertions.assertEquals/assertTrue/...`
  (static-import them), `assertThrows(IllegalArgumentException.class, () -> ...)`
  for error paths, `@ParameterizedTest` + `@CsvSource`/`@MethodSource` for
  input tables, `@BeforeEach` for shared setup.
- Doubles: prefer hand-written fakes via constructor injection. Use Mockito
  only if it's already in the pom — do not add new dependencies silently.
- Naming: `methodName_condition_expectedResult` or backtick-free descriptive
  names; match existing tests' style if any.

## Running and reading failures

- Whole suite: `run_command("mvn -q test")`.
- One class: `run_command("mvn -q test -Dtest=CalculatorTest")`;
  one method: `-Dtest=CalculatorTest#dividesByZeroThrows`.
- Compilation error (`COMPILATION ERROR` block) → wrong import/signature in
  the TEST; re-read the real signature with get_function_source.
- `No tests were executed` / tests not found → wrong directory or filename
  not matching surefire's `*Test.java` pattern.
- `NoClassDefFoundError`/`ClassNotFoundException` in tests → missing test-scope
  dependency; report it rather than adding dependencies unprompted.
- Full surefire detail lives in `target/surefire-reports/*.txt` — read it when
  the console summary is truncated.

## pom.xml policy

Never touch production source. `pom.xml` may be edited ONLY to add missing
test/coverage tooling (junit-jupiter test-scope dependency, jacoco-maven-plugin,
surefire version fix) — keep the diff minimal and state exactly what you added
in your report.
