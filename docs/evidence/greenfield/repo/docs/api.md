# API Reference

This document details the REST API endpoints exposed by the URL Shortener service.

## Endpoints Overview

| Method | Path | Summary |
|---|---|---|
| `POST` | `/shorten` | Create a shortened link |
| `GET` | `/{code}` | Redirect to original target URL |
| `GET` | `/analytics/{code}` | Fetch visit statistics for a code |
| `GET` | `/health` | Check service and database health |

---

## 1. Create Short Link

Creates a shortened link with an auto-generated Base62 code or a custom alias, and an optional expiration timestamp.

- **Path**: `/shorten`
- **Method**: `POST`
- **Headers**:
  - `Content-Type: application/json`
- **Rate Limiting**: Enforced per client remote IP address.

### Request Body

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | Yes | Target destination URL. Must use `http` or `https` scheme and resolve to a public IP address. |
| `custom_alias` | string | No | Custom short code. Must be 3–64 characters containing only alphanumeric characters, dashes (`-`), and underscores (`_`). |
| `expires_at` | string (ISO-8601 UTC) | No | Optional UTC timestamp when the link expires (e.g. `2026-01-01T00:00:00Z`). |

#### Example Request (Auto-generated Code)
```json
{
  "url": "https://example.com/very/long/path"
}
```

#### Example Request (Custom Alias & Expiry)
```json
{
  "url": "https://example.com/very/long/path",
  "custom_alias": "promo-2025",
  "expires_at": "2026-01-01T00:00:00Z"
}
```

### Responses

#### `201 Created`
Returned when the short link is successfully created.

```json
{
  "code": "promo-2025",
  "short_url": "http://localhost:5000/promo-2025"
}
```

#### `400 Bad Request`
Returned if the input payload is invalid, the URL uses an unsupported scheme, or the target resolves to a private, loopback, link-local, or multicast address (SSRF prevention).

```json
{
  "error": "Target URL resolves to a private or disallowed IP address."
}
```

#### `409 Conflict`
Returned if the requested `custom_alias` already exists in the system.

```json
{
  "error": "The custom alias 'promo-2025' is already in use."
}
```

#### `429 Too Many Requests`
Returned when the client IP exceeds the configured link creation threshold.

**Headers:**
- `Retry-After: <seconds>`

```json
{
  "error": "Rate limit exceeded. Please try again later."
}
```

---

## 2. Redirect to Target URL

Resolves a short code and redirects the client to the original target URL. If active, increments the link's `ClickCount` by 1 and updates `LastVisitedAt` to the current UTC timestamp.

- **Path**: `/{code}`
- **Method**: `GET`

### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code` | string | Yes | The generated short code or custom alias. |

### Responses

#### `307 Temporary Redirect`
Returned when the code exists and has not expired.

**Headers:**
- `Location: <original_target_url>`

*Note: An HTTP body is not returned with the redirect.*

#### `404 Not Found`
Returned when:
- The code does not exist.
- The link has expired (`DateTime.UtcNow > ExpiresAt`). (Click counts are not incremented for expired links).

```json
{
  "error": "Short link not found or expired."
}
```

---

## 3. Retrieve Link Analytics

Retrieves visit counts and lifecycle timestamps for a short link.

- **Path**: `/analytics/{code}`
- **Method**: `GET`

### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code` | string | Yes | The generated short code or custom alias. |

### Responses

#### `200 OK`
Returned when the code exists.

```json
{
  "total_clicks": 42,
  "created_at": "2025-05-10T12:00:00Z",
  "last_visited_at": "2025-05-10T12:45:10Z"
}
```

*Note:* If a link has never been visited, `total_clicks` is `0` and `last_visited_at` is `null`.

*Note on Expired Links:* Expired links remain queryable via `/analytics/{code}` to inspect historical metrics. Spec Open Question Q4 discusses including an optional `"expired": true` flag in the payload, but the baseline contract returns `total_clicks`, `created_at`, and `last_visited_at`.

#### `404 Not Found`
Returned if the code does not exist.

```json
{
  "error": "Short link not found."
}
```

---

## 4. Health Check

Verifies the operational status of the service process and backing SQLite database connection.

- **Path**: `/health`
- **Method**: `GET`

### Responses

#### `200 OK`
Returned when the service and database are fully operational.

```json
{
  "status": "healthy"
}
```

#### `503 Service Unavailable`
Returned when the backing SQLite datastore cannot be queried or is unreachable.

```json
{
  "status": "unhealthy",
  "error": "Database connection failure."
}
```