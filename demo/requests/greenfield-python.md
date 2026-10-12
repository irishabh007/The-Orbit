# Greenfield: URL shortener service

Build a URL shortener HTTP service from scratch.

Core API
- POST /shorten takes a long URL and returns a short code and the short URL. Callers may
  supply their own custom alias instead of a generated code; aliases must be unique.
- GET /{code} redirects to the original URL. Unknown or expired codes return 404.
- GET /analytics/{code} returns click statistics for a link: total clicks, when it was
  created, and when it was last visited.
- GET /health returns service health for load balancers.

Behaviour
- Links can optionally expire after a given time.
- Reject unsafe targets: only http and https URLs, and nothing that points at localhost or
  private network addresses (SSRF protection).
- Rate-limit link creation per client so the service cannot be abused.
- Data must survive a restart.

Engineering expectations
- Clean, typed, maintainable code a new engineer can continue.
- Automated tests for every endpoint, including error cases.
- A README with setup, run and test instructions plus an API reference.
