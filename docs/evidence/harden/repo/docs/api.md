# API Reference

REST API documentation for the URL Shortener service.

## Endpoints Overview

| Method | Path | Summary | Status Codes |
|---|---|---|---|
| `POST` | `/shorten` | Create a shortened link | `201`, `400`, `409`, `429` |
| `GET` | `/{code}` | Redirect to the destination URL; atomically increments visit counts | `307`, `404` |
| `GET` | `/analytics/{code}` | Retrieve lifetime visit metrics | `200`, `404` |
| `GET` | `/analytics/{code}/daily` | Retrieve contiguous daily click metrics | `200`, `400`, `404` |
| `GET` | `/health` | Check service and database connectivity | `200`, `503` |

---

## 1. Create Short Link

Creates a new shortened link, generating a 7-character Base62 code unless a valid custom alias is provided. Optional link expiration can be specified.

- **Path**: `/shorten`
- **Method**: `POST`
- **Headers**:
  - `Content-Type: application/json`
- **Rate Limiting**: Enforced via `ShortenPolicy` (default 60 requests / minute per client IP).

### Request Body

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | Yes | Target destination URL. Must be an absolute HTTP or HTTPS URL resolving to a public, non-restricted IP address. |
| `custom_alias` | string | No | Optional custom code. Must match regex `^[a-zA-Z0-9_-]{3,64}$` (3 to 64 alphanumeric characters, underscores, or hyphens). |
| `expires_at` | string (ISO-8601 UTC) | No | Optional expiration timestamp. Stored in UTC. |

#### Example Request
```json
{
  "url": "https://example.com/target-resource",
  "custom_alias": "my-custom-link",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

### Responses

#### `201 Created`
Returned when the short link is successfully created. Includes a `Location` header pointing to `/{code}`.

```json
{
  "code": "my-custom-link",
  "short_url": "http://localhost:5000/my-custom-link",
  "url": "https://example.com/target-resource",
  "created_at": "2026-03-30T10:00:00Z",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

#### `400 Bad Request`
Returned when the payload is missing, URL is missing or invalid, custom alias does not meet formatting requirements (3-64 characters matching `^[a-zA-Z0-9_-]+$`), or the URL fails SSRF validation (resolves to private, loopback, link-local, carrier-grade NAT, or restricted IP ranges).

```json
{
  "error": "Target URL resolves to a private or restricted network address (SSRF prevention).",
  "message": "Target URL resolves to a private or restricted network address (SSRF prevention)."
}
```

#### `409 Conflict`
Returned when the requested `custom_alias` is already in use by another link, or when an internal Base62 generation collision occurs after retry attempts.

```json
{
  "error": "The specified custom alias is already in use.",
  "message": "The specified custom alias is already in use."
}
```

#### `429 Too Many Requests`
Returned when the client IP exceeds the configured rate limiting quota.

**Response Headers:**
- `Retry-After`: Remaining seconds until the fixed rate-limit window resets.

```json
{
  "error": "Rate limit exceeded. Please try again later.",
  "message": "Rate limit exceeded."
}
```

---

## 2. Redirect to Target URL

Resolves the short code and returns a temporary redirect (`307`) to the target destination. On valid redirect, atomically updates the link's lifetime `ClickCount`, sets `LastVisitedAtUtc` to current UTC, and upserts the visit count for the current UTC date in `ShortLinkDailyClicks`.

- **Path**: `/{code}`
- **Method**: `GET`

### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code` | string | Yes | Short code or custom alias to resolve. Reserved names (`health`, `shorten`, `analytics`, case-insensitive) return 404. |

### Responses

#### `307 Temporary Redirect`
Returned when the link exists and has not expired (`ExpiresAtUtc` is null or greater than current UTC).

**Response Headers:**
- `Location`: Target URL

#### `404 Not Found`
Returned when the short code does not exist, the code is a reserved keyword, or the link has expired (`ExpiresAtUtc <= DateTime.UtcNow`). No database counter increments are performed.

```json
{
  "error": "Short code not found or link has expired.",
  "message": "Short code not found or link has expired."
}
```

---

## 3. Retrieve Lifetime Analytics

Retrieves aggregate visit metrics and metadata for a short link.

- **Path**: `/analytics/{code}`
- **Method**: `GET`

### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code` | string | Yes | The short code or custom alias. |

### Responses

#### `200 OK`
Returned when the short link exists and has not expired.

```json
{
  "code": "my-custom-link",
  "url": "https://example.com/target-resource",
  "total_clicks": 42,
  "created_at": "2026-03-30T10:00:00Z",
  "last_visited_at": "2026-03-30T10:30:00Z",
  "expires_at": "2026-12-31T23:59:59Z",
  "expired": false
}
```

*Note:* If the link has not yet received redirects, `total_clicks` is `0` and `last_visited_at` is `null`.

#### `404 Not Found`
Returned if the short code does not exist or has expired (`ExpiresAtUtc <= DateTime.UtcNow`).

```json
{
  "error": "Short code not found.",
  "message": "Short code not found."
}
```

---

## 4. Retrieve Daily Analytics

Retrieves daily click analytics over a contiguous UTC date window ending on the current UTC date. Inactive dates within the window are zero-filled.

- **Path**: `/analytics/{code}/daily`
- **Method**: `GET`

### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code` | string | Yes | The short code or custom alias. |

### Query Parameters

| Parameter | Type | Required | Default | Allowed Values | Description |
|---|---|---|---|---|---|
| `days` | integer | No | `7` | `1` to `90` | Number of consecutive UTC calendar days ending on current UTC date. |

### Responses

#### `200 OK`
Returned when the short link exists and has not expired. The `daily` array contains contiguous dates sorted in chronological ascending order.

```json
{
  "code": "my-custom-link",
  "totalClicks": 42,
  "days": 7,
  "daily": [
    { "date": "2026-03-24", "clicks": 0 },
    { "date": "2026-03-25", "clicks": 0 },
    { "date": "2026-03-26", "clicks": 5 },
    { "date": "2026-03-27", "clicks": 12 },
    { "date": "2026-03-28", "clicks": 8 },
    { "date": "2026-03-29", "clicks": 10 },
    { "date": "2026-03-30", "clicks": 7 }
  ]
}
```

#### `400 Bad Request`
Returned when the `days` parameter cannot be parsed as an integer or is outside the range `[1, 90]`.

```json
{
  "error": "The days parameter must be an integer between 1 and 90.",
  "message": "The days parameter must be an integer between 1 and 90."
}
```

#### `404 Not Found`
Returned when the short code does not exist or has expired.

```json
{
  "error": "Short code not found.",
  "message": "Short code not found."
}
```

---

## 5. Health Check

Verifies process availability and database query responsiveness.

- **Path**: `/health`
- **Method**: `GET`

### Responses

#### `200 OK`
Returned when `dbContext.Database.CanConnectAsync()` succeeds and a test query against `ShortLinks` executes.

```json
{
  "status": "healthy"
}
```

#### `503 Service Unavailable`
Returned when database connectivity check fails or throws an unhandled database exception.

```json
{
  "status": "unhealthy"
}
```