"""Local browser dashboard for Praxist operators.

The dashboard is an operator surface over the existing registry, monitor, and
CLI lifecycle contracts.  It does not own research-loop state.  Imports stay
lazy so the top-level CLI can register this package without a module cycle.
"""
