## What was broken

<!-- The behaviour before this change, and why it was wrong. The diff already
     says what changed; this says why it needed to. -->

## What this does

<!-- The shape of the fix, and any alternative you rejected and why. -->

## Verification

<!-- What you actually ran, and what it printed. Not "tests pass" - which tests,
     and what they now cover that they did not before. -->

- [ ] `make lint type test` green for every component touched
- [ ] `go test -race ./...` if the worker changed
- [ ] `make protos` rerun and the result committed, if a proto changed
- [ ] Test added that fails without this change

## Contract changes

<!-- Delete this section if none. Otherwise say which, and what an operator has
     to do about it. -->

- [ ] Proto field added (new number, nothing renumbered or reused)
- [ ] Executor protocol version bumped, both ends and the golden fixture updated
- [ ] Generation manifest changed - existing artifacts need regenerating
- [ ] Configuration variable added or renamed, and documented in the component README
