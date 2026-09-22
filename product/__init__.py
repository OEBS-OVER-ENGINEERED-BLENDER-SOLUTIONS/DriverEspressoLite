"""Identity and catalogue membership for this build.

The catalogue this build ships is frozen in
`catalogue/core/frozen_records.py`, and the runtime reads it from there, so
membership cannot drift from what the package actually contains.
"""
