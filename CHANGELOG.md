# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project is pre-1.0: breaking changes may land in minor releases.

## [Unreleased]

### Changed

- REST calls now use `api.bluecurrent.nl` instead of the legacy `bo.bluecurrent.nl` backoffice host (same `bc_api` v2.0 API and response shapes).

## [0.1.1] - 2026-07-04

### Fixed

- `get_account` no longer raises a `ValueError` on BlueCurrent's current date format; `first_login_app` now parses both the legacy (`01-JAN-20`) and ISO (`2020-01-15T13:33:52`) formats.

### Changed

- `get_account` returns `first_login_app` as a `datetime` (previously a `date`).
- Internal: switched tooling to Ruff and ty, added a Python 3.10–3.13 CI matrix, and moved to PyPI trusted publishing (OIDC).

[Unreleased]: https://github.com/rogiervandergeer/pybluecurrent/compare/0.1.1...HEAD
[0.1.1]: https://github.com/rogiervandergeer/pybluecurrent/compare/0.1.0...0.1.1
