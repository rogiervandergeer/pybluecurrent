# pybluecurrent

Python client for [BlueCurrent](https://www.bluecurrent.nl) charge points.

![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/rogiervandergeer/pybluecurrent/test.yaml) 
![PyPI](https://img.shields.io/pypi/v/pybluecurrent)
![PyPI - License](https://img.shields.io/pypi/l/pybluecurrent)
![PyPI - Downloads](https://img.shields.io/pypi/dm/pybluecurrent) 

`pybluecurrent` is an **unofficial, third-party async client** — it is not affiliated with
BlueCurrent.

Compared to BlueCurrent's official [`bluecurrent-api`](https://github.com/bluecurrent/HomeAssistantAPI):

- **Per-call `async`/`await`** — each method awaits its own response, rather than a single callback
  receiver that routes every server message.
- **Typed responses** — getters return `TypedDict`-annotated dictionaries; the official client
  hands back untyped dicts.
- **Instance-scoped state** — no process-global mutable state.
- **Username/password *or* API token** — the official client is API-token only.

## Usage

Using the client is as simple as:
```python
from pybluecurrent import BlueCurrentClient

client = BlueCurrentClient("your_username", "your_secret_password")

async with client:
    charge_points = await client.get_charge_points()
    transactions = await client.get_transactions(charge_points[0]["evse_id"])
```

### Connection

The client can only be used while its websocket is connected. For example:
```python
client = BlueCurrentClient("your_username", "your_secret_password")
async with client:
    result = await client.get_account()
```
Entering the async context automatically logs in.

Instead of a username and password, you can authenticate with an API token:
```python
client = BlueCurrentClient(api_token="your_api_token")
```
Retrieve or rotate the token with [`get_api_token`](#get_api_token) and
[`generate_api_token`](#generate_api_token), or from the [BlueCurrent website](https://my.bluecurrent.nl).

## Methods

Every method is a coroutine on `BlueCurrentClient`; call them inside the async context (see
[Connection](#connection)). Charge points are addressed by their `evse_id`.

- **Account & authentication** — [`get_account`](#get_account), [`get_api_token`](#get_api_token), [`generate_api_token`](#generate_api_token), [`get_contracts`](#get_contracts)
- **Charge points & cards** — [`get_charge_points`](#get_charge_points), [`get_charge_point_settings`](#get_charge_point_settings), [`get_charge_point_status`](#get_charge_point_status), [`get_charge_cards`](#get_charge_cards)
- **Grid & sustainability** — [`get_grid_status`](#get_grid_status), [`get_grids`](#get_grids), [`get_sustainability_status`](#get_sustainability_status)
- **Settings & control** — [`set_plug_and_charge_charge_card`](#set_plug_and_charge_charge_card), [`set_status`](#set_status), [`soft_reset`](#soft_reset)
- **Smart charging** — [`set_delayed_charging`](#set_delayed_charging), [`set_delayed_charging_schedule`](#set_delayed_charging_schedule), [`set_price_based_charging`](#set_price_based_charging), [`set_price_based_charging_settings`](#set_price_based_charging_settings), [`boost`](#boost)
- **Transactions** — [`get_transactions`](#get_transactions), [`iterate_transactions`](#iterate_transactions)

### Response models

The getters return plain dictionaries annotated with `TypedDict`s from
[`pybluecurrent.models`](https://github.com/rogiervandergeer/pybluecurrent/blob/main/src/pybluecurrent/models.py).
Access is unchanged — `response["key"]`, `.get()`, `**response` and `json.dumps` all keep working —
and an unexpected field the backend adds simply rides along; the types just add autocomplete and
static checking:

```python
from pybluecurrent.models import ChargePoint, Transaction
```

**The model definitions are the field-level reference** — each field, its type, and any parsing
notes live there. The response types are `Account`, `ChargeCard`, `ChargePoint`,
`ChargePointSettings`, `ChargePointStatus`, `GridStatus`, `Grid`, `SustainabilityStatus`,
`Contract`, `TransactionsPage` and `Transaction`, built from the nested shapes `Tariff`,
`Location`, `Address`, `DelayedCharging`, `PriceBasedCharging`, `CardRef`, `BoolSetting` and
`IntSetting`. Dates and times are parsed for you: `date`/`datetime` fields are Python objects, and
schedule times (`start_time`, `end_time`, `expected_departure_time`) are `datetime.time`.

### Account & authentication

#### get_account

```python
async def get_account(self) -> Account
```

Returns your account information as an [`Account`](#response-models).

#### get_api_token

```python
async def get_api_token(self) -> str
```

Returns the API token (home automation key) for your account. It can be used to authenticate
instead of a username and password, by constructing the client with `BlueCurrentClient(api_token=...)`.

#### generate_api_token

```python
async def generate_api_token(self) -> str
```

Generates a new API token and returns it. **Warning:** this rotates the token — any previously
issued token is invalidated, which will break anything still using the old one.

#### get_contracts

```python
async def get_contracts(self) -> list[Contract]
```

Returns your contracts, each a [`Contract`](#response-models).

### Charge points & cards

#### get_charge_points

```python
async def get_charge_points(self) -> list[ChargePoint]
```

Returns your charge points, each a [`ChargePoint`](#response-models). A disabled smart-charging
profile is still present as its `{value, permission}` wrapper; its schedule/settings fields appear
only while the profile is enabled.

#### get_charge_point_settings

```python
async def get_charge_point_settings(self, evse_id: str) -> ChargePointSettings
```

Returns the settings of a charge point as a [`ChargePointSettings`](#response-models). All of this
is already included in the response of [`get_charge_points`](#get_charge_points).

**Arguments**
- `evse_id`: The ID of the charge point.

#### get_charge_point_status

```python
async def get_charge_point_status(self, evse_id: str) -> ChargePointStatus
```

Returns the live status of a charge point as a [`ChargePointStatus`](#response-models).

**Arguments**
- `evse_id`: The ID of the charge point.

#### get_charge_cards

```python
async def get_charge_cards(self) -> list[ChargeCard]
```

Returns your charge cards, each a [`ChargeCard`](#response-models).

### Grid & sustainability

#### get_grid_status

```python
async def get_grid_status(self, evse_id: str) -> GridStatus
```

Returns the grid status associated with a charge point (currents in amps) as a
[`GridStatus`](#response-models).

**Arguments**
- `evse_id`: The ID of the charge point.

#### get_grids

```python
async def get_grids(self) -> list[Grid]
```

Returns your grid connections, each a [`Grid`](#response-models).

#### get_sustainability_status

```python
async def get_sustainability_status(self) -> SustainabilityStatus
```

Returns sustainability statistics for all your charge points as a
[`SustainabilityStatus`](#response-models) — `{"trees": ..., "co2": ...}`.

### Settings & control

#### set_plug_and_charge_charge_card

```python
async def set_plug_and_charge_charge_card(self, evse_id: str, uid: str | None = None) -> None
```

Sets the plug-and-charge card for the charge point. `uid` must be the `uid` of one of your
[charge cards](#get_charge_cards), or `None` to charge without a card. Raises `BlueCurrentException`
if the command fails.

**Arguments**
- `evse_id`: The ID of the charge point.
- `uid`: A charge card UID, or `None` (the default) to use no charge card.

#### set_status

```python
async def set_status(self, evse_id: str, enabled: bool) -> None
```

Enables or disables a charge point. Raises `BlueCurrentException` if the command fails.

**Arguments**
- `evse_id`: The ID of the charge point.
- `enabled`: Boolean that indicates the desired status.

#### soft_reset

```python
async def soft_reset(self, evse_id: str) -> None
```

Soft-resets a charge point. Raises `BlueCurrentException` if the command fails.

**Arguments**
- `evse_id`: The ID of the charge point.

### Smart charging

#### set_delayed_charging

```python
async def set_delayed_charging(self, evse_id: str, enabled: bool) -> None
```

Enables or disables delayed charging. While enabled, the charge point only charges within the window
configured with [`set_delayed_charging_schedule`](#set_delayed_charging_schedule), and delays
charging outside of it. A charge point has at most one smart-charging profile active, so enabling
this disables any other profile.

**Arguments**
- `evse_id`: The ID of the charge point.
- `enabled`: Whether delayed charging should be enabled.

#### set_delayed_charging_schedule

```python
async def set_delayed_charging_schedule(
    self,
    evse_id: str,
    start_time: time | str,
    end_time: time | str,
    days: Iterable[Weekday | int | str],
) -> None
```

Sets the window in which the charge point may charge on the selected days. The window may span
midnight. It is applied only while delayed charging is enabled with
[`set_delayed_charging`](#set_delayed_charging).

```python
from datetime import time
from pybluecurrent import Weekday

await client.set_delayed_charging_schedule(
    "BCU123456", start_time=time(23, 0), end_time=time(7, 0), days=[Weekday.MONDAY, "tu", 3]
)
```

**Arguments**
- `evse_id`: The ID of the charge point.
- `start_time`: The time at which charging may start, as a `time` or a `"HH:MM"` string.
- `end_time`: The time at which charging must stop, as a `time` or a `"HH:MM"` string.
- `days`: The days on which the schedule applies. Each day may be a `pybluecurrent.Weekday`, an
  isoweekday number (1 for Monday through 7 for Sunday), or a name such as `"monday"` or `"mo"`.

The schedule is read back from the `delayed_charging` key of
[`get_charge_point_settings`](#get_charge_point_settings).

#### set_price_based_charging

```python
async def set_price_based_charging(self, evse_id: str, enabled: bool) -> None
```

Enables or disables price-based charging. While enabled, the charge point charges during the
cheapest hours before the expected departure time, as configured with
[`set_price_based_charging_settings`](#set_price_based_charging_settings). A charge point has at most
one smart-charging profile active, so enabling this disables any other profile.

**Arguments**
- `evse_id`: The ID of the charge point.
- `enabled`: Whether price-based charging should be enabled.

#### set_price_based_charging_settings

```python
async def set_price_based_charging_settings(
    self,
    evse_id: str,
    expected_departure_time: time | str,
    expected_kwh: float,
    minimum_kwh: float,
) -> None
```

Configures how much energy to charge before departure. Applied only while price-based charging is
enabled with [`set_price_based_charging`](#set_price_based_charging).

**Arguments**
- `evse_id`: The ID of the charge point.
- `expected_departure_time`: The time the vehicle is expected to leave, as a `time` or a `"HH:MM"`
  string.
- `expected_kwh`: The amount of energy, in kWh, expected to be charged before departure.
- `minimum_kwh`: The amount of energy, in kWh, to charge immediately regardless of price.

The settings are read back from the `price_based_charging` key of
[`get_charge_point_settings`](#get_charge_point_settings).

#### boost

```python
async def boost(self, evse_id: str) -> None
```

Starts charging immediately, overriding whichever smart-charging profile is currently delaying
charging — delayed charging or price-based charging — for the ongoing session. The override cannot
be undone. While it is active, [`get_charge_point_status`](#get_charge_point_status) reports
`"boosting": True`. Raises `ValueError` if no smart-charging profile is active.

**Arguments**
- `evse_id`: The ID of the charge point.

### Transactions

#### get_transactions

```python
async def get_transactions(self, evse_id: str, newest_first: bool = True, page: int = 1) -> TransactionsPage
```

Returns a single page of transactions as a [`TransactionsPage`](#response-models); its
`transactions` key holds a list of [`Transaction`](#response-models).

**Arguments**
- `evse_id`: The ID of the charge point.
- `newest_first`: If `True`, start with the most recent transaction. Defaults to `True`.
- `page`: Page number to get. Defaults to `1`.

#### iterate_transactions

```python
async def iterate_transactions(self, evse_id: str, newest_first: bool = True) -> AsyncIterable[Transaction]
```

Iterates over all your transactions, fetching further pages as needed. Yields
[`Transaction`](#response-models) dictionaries.

**Arguments**
- `evse_id`: The ID of the charge point.
- `newest_first`: If `True`, start with the most recent transaction. Defaults to `True`.

## Development

- **Install** (editable, with dev extras): `uv sync --extra dev` (or `pip install -e ".[dev]"`).
- **Pre-commit**: the repo ships a `.pre-commit-config.yaml`, but git installs no hooks on clone, so
  it is a one-time manual step — run `uvx pre-commit install`.
- **Contributions** and feature requests are welcome.

## Changelog

See [CHANGELOG.md](https://github.com/rogiervandergeer/pybluecurrent/blob/main/CHANGELOG.md).
