---
name: coverage-jacoco
description: How to measure Java line coverage with JaCoCo under Maven, produce the XML report, and read it to decide which tests to write next.
---

# Coverage measurement with JaCoCo (Maven)

## Standard flow

1. Check `pom.xml` for `jacoco-maven-plugin`. If missing, add the minimal
   plugin block (this is the one allowed pom edit — report it):

   ```xml
   <plugin>
     <groupId>org.jacoco</groupId>
     <artifactId>jacoco-maven-plugin</artifactId>
     <version>0.8.12</version>
     <executions>
       <execution>
         <goals><goal>prepare-agent</goal></goals>
       </execution>
       <execution>
         <id>report</id>
         <phase>test</phase>
         <goals><goal>report</goal></goals>
       </execution>
     </executions>
   </plugin>
   ```

2. Run tests with the agent attached and generate the report:
   `run_command("mvn -q test jacoco:report")`
   (add `-Dmaven.test.failure.ignore=true` when you want a coverage snapshot
   even though some tests still fail).
3. Parse: `jacoco_report(xml_file="target/site/jacoco/jacoco.xml",
   source_filter="com/mycompany")` — returns total %, per-file %,
   `uncovered_lines`, `uncovered_functions` (Class.method), same schema as the
   C++/Python reports.

## Reading the report

- `uncovered_functions` — methods never executed: highest-value targets; write
  at least one test each. `constructor` means the class was never instantiated.
- Clusters of `uncovered_lines` inside a covered method = untaken branches:
  `else` arms, `catch` blocks, early returns, switch cases. Map lines back to
  methods with analyze_source_file, then force the branch (invalid input,
  boundary value, exception-triggering argument).
- Don't chase 100%: `main()`, trivial DTO boilerplate, and defensive
  `throw new AssertionError("unreachable")` lines are acceptable gaps — list
  them in the final report instead.

## Gotchas

- `jacoco.exec` / report missing after tests ran → `prepare-agent` didn't run
  (plugin missing or executions misconfigured); fix pom per step 1 and rerun.
- Surefire `argLine` overridden in pom (e.g. for memory flags) silently drops
  the JaCoCo agent — merge with `@{argLine}` instead of replacing it.
- Multi-module projects: each module writes its own
  `<module>/target/site/jacoco/jacoco.xml`; parse per module (or use
  `jacoco:report-aggregate` if configured).
- Coverage is bytecode-based: compiler-generated code (synthetic methods,
  string-switch tables) can produce lines you can't cover from source — note,
  don't fight.
