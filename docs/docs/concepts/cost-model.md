---
title: Cost model
sidebar_position: 7
description: How Readie weighs checkpoint size against import time with the alpha weight, including a worked example.
---

The cost model is the rule that Readie uses to compare checkpoints and cold starts. A larger checkpoint holds more packages but takes longer to restore. The weight `alpha` expresses the restore cost in seconds per megabyte of checkpoint. Readie uses it both when building checkpoints and when serving a call, and it chooses the option with the lowest total time.

## Trade-off

Two terms add up to the time a call spends getting ready:

1. **Restore time.** Loading a checkpoint into memory takes longer the larger the checkpoint is. Readie models this as `alpha × size`, where `size` is the memory footprint of the checkpoint in MB and `alpha` is in seconds per MB.
2. **Leftover imports.** Any package that the function needs and the checkpoint does not hold must still be imported after the restore. This costs the measured import time of each such package.

The cost of using a checkpoint for a call is:

```text
cost = alpha × size(checkpoint)
     + sum of import time of every needed package the checkpoint lacks
```

The needed packages include everything they depend on. A dependency shared by two needed packages is counted once.

A cold start has no checkpoint, so its cost is the import time of everything needed. A checkpoint is worth using only if its cost is lower.

## Worked example

The numbers in this example are illustrative and chosen to show the arithmetic. Real values come from measurements in the [catalogue](/docs/concepts/flavors-and-catalogues).

A call needs `pandas`, which depends on `numpy`. The import times are 0.2 s for `numpy` and 0.5 s for `pandas`, so the cold start costs 0.7 s.

Three checkpoints exist, and `alpha` is 0.006 seconds per MB (an illustrative value; the real value is measured and changes between builds):

| Option | Size | Size cost (0.006 × size) | Imports still needed | Total |
| --- | --- | --- | --- | --- |
| Cold start | 0 MB | 0 | numpy, pandas = 0.7 s | 0.70 s |
| A: numpy | 7 MB | 0.042 s | pandas = 0.5 s | 0.542 s |
| B: numpy, pandas | 80 MB | 0.48 s | none | 0.48 s |
| C: numpy, pandas, torch | 200 MB | 1.20 s | none | 1.20 s |

Checkpoint B wins at 0.48 s. Checkpoint C holds everything the call needs, but its extra 120 MB costs more than it saves.

If `alpha` is 0.02, restoring memory is more expensive. Checkpoint A then costs 0.14 + 0.5 = 0.64 s, B costs 1.6 s, C costs 4.0 s, and the cold start costs 0.70 s. Checkpoint A wins. A larger `alpha` favors smaller checkpoints.

## Ties

If two options cost exactly the same, the router picks the one that comes later in the catalogue. The empty `checkpoint_0` has size 0 and holds nothing, so its cost equals the cold-start cost. It therefore wins ties against a cold start. When a catalogue is loaded, the router chooses a restore of `checkpoint_0` over a true cold start unless a better checkpoint exists.

Only packages that the catalogue has measured are priced. An unknown package adds the same amount to every option, so it cannot change which option wins.

## Location of alpha

`alpha` appears on both sides of the system. Both sides must describe the same trade-off, but the value is not configured in two places.

### Pipeline

The pipeline runs offline and handles `alpha` as follows:

- The setting is `READIE_ALPHA`. It has a built-in default, and the repository Makefile passes its own value when `make capture` or `make generation` runs. Read `READIE_ALPHA` in those two places and do not rely on a number written in a document, because the value is tuned over time.
- The value is rounded to six decimal places and must be greater than zero.
- The greedy planner scores candidate checkpoints with the same cost formula, summed over the example requests in its corpus. It keeps adding checkpoints while the total cost drops and the size budget allows. See [Building checkpoints](/docs/architecture/building-checkpoints).
- After capture, the pipeline restores every checkpoint and times it. It fits a straight line through the (size, restore time) points and takes the slope. This measured slope is the `alpha` that the pipeline writes into the catalogue. The pipeline prints the planned value, the measured value, and the difference, which shows the drift.

### Router

The router runs online and handles `alpha` as follows:

- The router has no `alpha` setting and no `ALPHA` environment variable.
- It reads the `alpha` field from each catalogue file when it loads the catalogue. If the field is missing, it uses a built-in fallback.
- It multiplies the `size_mb` of each checkpoint by that value once, at load time.

The router therefore prices size with the value that the pipeline measured on the generation it reads. To change the weight, rebuild the generation with a different `READIE_ALPHA` and give the router the new catalogue. The catalogue file shows the value with which a given generation was built.

## What's next

- [Checkpoints](/docs/concepts/checkpoints)
- [Flavors and catalogues](/docs/concepts/flavors-and-catalogues)
- [Placement](/docs/architecture/placement)
