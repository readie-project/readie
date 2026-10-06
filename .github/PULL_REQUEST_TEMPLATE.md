## What was broken

<!-- The behavior before this change, and why it was wrong. The diff already
     shows what changed; this section explains why the change was needed. -->

## What this does

<!-- The shape of the fix, and any alternative you rejected and why. -->

## Verification

<!-- What you ran, and what it printed. Name the tests, and state what they
     now cover that they did not before. "Tests pass" is not sufficient. -->

- [ ] `make lint type test` green for every component touched
- [ ] `go test -race ./...` if the worker changed
- [ ] `make protos` rerun and the result committed, if a proto changed
- [ ] Test added that fails without this change

## Contract changes

<!-- Delete this section if there are none. Otherwise state which, and what an
     operator must do about it. -->

- [ ] Proto field added (new number, nothing renumbered or reused)
- [ ] Executor protocol version bumped, both ends and the golden fixture updated
- [ ] Generation manifest changed: existing artifacts need regenerating
- [ ] Configuration variable added or renamed, and documented in the component README
