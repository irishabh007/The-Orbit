# Brownfield: daily click analytics for the existing URL shortener

The URL shortener is already in production (the existing code is provided). Marketing wants
to see click trends over time.

Change request
- Add GET /analytics/{code}/daily returning click counts per UTC day for the last N days.
  N comes from an optional `days` query parameter: default 7, maximum 90; days without
  clicks are reported as zero.
- Unknown or expired codes behave exactly like the existing analytics endpoint.

Constraints
- Every existing endpoint must keep its current behaviour and response format.
- Existing links must keep their current click totals; existing data must not be lost.
- All existing tests must keep passing; add tests for the new endpoint.
- Update the README / API docs for the new endpoint.
