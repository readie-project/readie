"""HTTP service behind the docs playground.

The service forwards a visitor's source text to Readie and streams the output
back. It never executes that text itself; see ``wrapper``.
"""
