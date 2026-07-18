# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project is pre-1.0: breaking changes may land in minor releases.

## [Unreleased]

### Added

- Automatic reconnection. A long-lived client now keeps itself connected: when the websocket drops it reconnects in the background with exponential backoff, reusing the cached session token. Calls made while reconnecting block until the connection is restored (bounded by `reconnect_wait_timeout`) rather than failing; calls already in flight when the drop happens still raise `ConnectionLost`. Tunable via the `auto_reconnect` (default on) and `reconnect_*` class attributes. Reconnection gives up after too many attempts, or immediately on rejected credentials, rather than retrying forever.
- Delayed (time-window) smart charging: `set_delayed_charging()` to switch the profile on or off, and `set_delayed_charging_schedule()` to set the window and the days it applies to.
- Price-based (dynamic-tariff) smart charging: `set_price_based_charging()` to switch the profile on or off, and `set_price_based_charging_settings()` to set the expected departure time and energy.
- `boost()` to charge now, overriding whichever smart charging profile (delayed or price-based) is currently active.
- `Weekday`, an `IntEnum` numbered like `date.isoweekday()`. Days can also be given as plain numbers or as names (`"monday"`, `"mo"`), so importing it is optional.
- `ConnectionLost` and `RequestTimeout` exceptions. Both derive from `BlueCurrentException`; `RequestTimeout` also subclasses the builtin `TimeoutError` (so existing `except TimeoutError` keeps working), and `AuthenticationFailed` now derives from `BlueCurrentException` as well as `ValueError`.

### Changed

- Websocket connection failures are now surfaced instead of hanging. When the handler stops (the connection drops, or an unexpected error), pending and subsequent calls raise `ConnectionLost` immediately rather than blocking until they time out. A single malformed (non-JSON) frame is now logged and skipped instead of silently killing the connection.
- `_receive` now enforces a single per-call deadline (a stream of unrelated frames can no longer postpone it indefinitely) and raises `RequestTimeout` rather than a bare `TimeoutError`.
- `set_status()`, `unlock_connector()` and `soft_reset()` now raise `BlueCurrentException` when the command fails (a `STATUS_` frame with `success: false`), instead of `set_status()` returning silently or the others handing back the failed frame. Their wait for that verdict is also longer (a new `command_timeout`, default 60s), so the backend's own ~30s answer is no longer cut off just before it arrives.

### Fixed

- The client no longer leaks the websocket, handler task, or HTTP client when connecting fails partway (for example on a rejected login), and teardown now awaits the handler and closes the HTTP client even if closing the websocket errors.

## [0.2.0] - 2026-07-11

### Added

- API-token authentication: construct the client with `BlueCurrentClient(api_token=...)` instead of a username and password.
- `get_api_token()` and `generate_api_token()` to fetch or rotate your account's API token (home automation key).

### Changed

- REST calls now use `api.bluecurrent.nl` instead of the legacy `bo.bluecurrent.nl` backoffice host (same `bc_api` v2.0 API and response shapes).
- REST requests now use a 30-second timeout instead of httpx's 5-second default, so occasional slow backend responses no longer raise `httpx.ReadTimeout`; override with the `http_timeout` attribute.
- Internal: added an offline websocket test harness (fake socket + recorded fixtures) so the auth/`_send`/`_receive`/`_handler` logic runs in CI without live credentials.

### Fixed

- Concurrent websocket calls are now safe: calls awaiting the same response type are serialized so overlapping same-type calls can no longer receive each other's replies (different-type calls still run concurrently). Errors the backend tags with a request id are routed to the originating call rather than failing every in-flight call.

## [0.1.1] - 2026-07-04

### Fixed

- `get_account` no longer raises a `ValueError` on BlueCurrent's current date format; `first_login_app` now parses both the legacy (`01-JAN-20`) and ISO (`2020-01-15T13:33:52`) formats.

### Changed

- `get_account` returns `first_login_app` as a `datetime` (previously a `date`).
- Internal: switched tooling to Ruff and ty, added a Python 3.10–3.13 CI matrix, and moved to PyPI trusted publishing (OIDC).

[Unreleased]: https://github.com/rogiervandergeer/pybluecurrent/compare/0.2.0...HEAD
[0.2.0]: https://github.com/rogiervandergeer/pybluecurrent/compare/0.1.1...0.2.0
[0.1.1]: https://github.com/rogiervandergeer/pybluecurrent/compare/0.1.0...0.1.1
