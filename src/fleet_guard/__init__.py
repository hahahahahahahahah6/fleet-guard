"""fleet-guard: machine-resource guard layer for fleets of coding-agent sessions.

Quotas background processes machine-wide, watches for stalled runs, reaps
leaked children on session end, and guards agent scratch-disk usage.
Everything is fail-open: a guard that crashes must never take down the agent.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
