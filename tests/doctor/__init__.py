"""Boundary tests for Phase 1: doctor, ci_lint, session_start, probe_run, the GitHub scripts and templates.

Everything runs offline against a temp HOME with fake credentials planted in it. Nothing reads the
real home, calls the network, runs `gh`, `aws` or `ssh`, or executes the real `claude`.
"""
