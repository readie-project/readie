"""Time how long corpus code takes to run through Readie, with and without checkpoint restore.

The evaluation is a Python application, run by whoever owns the Readie stack:
``readie-pipeline eval run`` sends each item of a corpus-format file to the
playground's ``/eval/run`` route and records the durations the server returns;
``readie-pipeline eval report`` summarises them. It is separate from corpus
generation and reads whatever file it is given, which is how an eval set differs
from the corpus the checkpoints were planned from.
"""
