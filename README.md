# pybluecurrent

Python client for [BlueCurrent](https://www.bluecurrent.nl) charge points.

![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/rogiervandergeer/pybluecurrent/test.yaml) 
![PyPI](https://img.shields.io/pypi/v/pybluecurrent)
![PyPI - License](https://img.shields.io/pypi/l/pybluecurrent)
![PyPI - Downloads](https://img.shields.io/pypi/dm/pybluecurrent) 

## Usage

Using the client is as simple as:
```python
from pybluecurrent import BlueCurrentClient

client = BlueCurrentClient("your_username", "your_secret_password")

async with client:
    charge_points = await client.get_charge_points()
    transactions = await client.get_transactions(charge_points[0]["evse_id"])
```

## Methods

The `BlueCurrentClient` exposes the following methods:
  
- [`get_account`](#getaccount---get-your-account-information)
- [`get_api_token`](#getapitoken---get-your-api-token)
- [`generate_api_token`](#generateapitoken---generate-a-new-api-token)
- [`get_charge_cards`](#getchargecards---get-your-charge-cards)
- [`get_charge_points`](#getchargepoints---get-your-charge-points)
- [`get_charge_point_settings`](#getchargepointsettings---get-the-settings-of-a-charge-point)
- [`get_grid_status`](#getgridstatus---get-the-grid-status-associated-to-a-charge-point)
- [`get_sustainability_status`](#getsustainabilitystatus---get-statistics-on-the-sustainability-of-all-your-charge-points)
- [`set_plug_and_charge_charge_card`](#setplugandchargechargecard---set-the-charge-card-for-plug-and-charge)
- [`set_status`](#setstatus---enable-or-disable-a-charge-point)
- [`login`](#login---log-in)
- [`get_charge_point_status`](#getchargepointstatus---get-the-status-of-a-charge-point)
- [`set_delayed_charging`](#setdelayedcharging---enable-or-disable-delayed-charging)
- [`set_delayed_charging_schedule`](#setdelayedchargingschedule---set-the-delayed-charging-schedule)
- [`boost`](#boost---charge-now-overriding-the-delayed-charging-window)
- [`get_contracts`](#getcontracts---get-your-contracts)
- [`get_grids`](#getgrids---get-your-grid-connections)
- [`get_transactions`](#gettransactions---get-a-list-of-transactions)
- [`iterate_transactions`](#iteratetransactions---iterate-through-your-transactions)

### Connection

The client can only be used when the websocket client is connected. For example:
```python
client = BlueCurrentClient("your_username", "your_secret_password")
async with client:
    result = await client.get_account()
```
Entering the async context will automatically login.

Instead of a username and password, you can authenticate with an API token:
```python
client = BlueCurrentClient(api_token="your_api_token")
```
Retrieve or rotate the token with [`get_api_token`](#getapitoken---get-your-api-token) and
[`generate_api_token`](#generateapitoken---generate-a-new-api-token), or from the
[BlueCurrent website](https://my.bluecurrent.nl).

#### `get_account` - Get your account information.

```python
async def get_account(self) -> dict[str, bool | str]
```

##### Returns
A dictionary describing your account:

```python
{
    "full_name": "Your Full Name",
    "email": "your@email.address",
    "login": "your@email.address",
    "should_reset_password": False,
    "developer_mode_enabled": False,
    "tel": "",
    "marketing_target": "bluecurrent",
    "first_login_app": datetime(2020, 1, 15, 13, 33, 52),
    "hubspot_user_identity": "a_very_long_string"
}
```

#### `get_api_token` - Get your API token.

```python
async def get_api_token(self) -> str
```

Returns the API token (home automation key) for your account. This token can be used to authenticate
instead of a username and password, by constructing the client with `BlueCurrentClient(api_token=...)`.

#### `generate_api_token` - Generate a new API token.

```python
async def generate_api_token(self) -> str
```

Generates a new API token and returns it. **Warning:** this rotates the token — any previously issued
token is invalidated, which will break anything still using the old one (for example a Home Assistant integration).

#### `get_charge_cards` - Get your charge cards.

```python
async def get_charge_cards(self) -> list[dict[str, date | int | str | None]]
```

##### Returns

A list of dictionaries, each representing a charge card:
```python
{
    "uid": "A1B2C3D4E5F6",
    "id": "NL-ABC-123456-0",
    "name": "My Charge Cards",
    "customer_name": "Your Name",
    "valid": 1,
    "date_created": date(2023, 6, 27),
    "date_modified": date(2023, 7, 11),
    "date_became_invalid": None
}
```

#### `get_charge_points` - Get your charge points.

```python
async def get_charge_points(self) -> list[dict[str, bool | dict | str]]
```

##### Returns

A list of dictionaries, each representing a charge card:
```python
{
    "evse_id": "BCU123456",
    "name": "",
    "model_type": "H:MOVE-C32T2",
    "chargepoint_type": "HIDDEN",
    "is_cable": True,
    "public_charging": {"value": False, "permission": "write"},
                    "default_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                     "customer_name": "Your Name", "valid": 1},
    "preferred_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                       "customer_name": "Your Name", "valid": 1},
    "plug_and_charge_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                             "customer_name": "Your Name", "valid": 1},
    "tariff":  {"tariff_id","NLBCUT58", "price_ex_vat": 0.2, "start_price_ex_vat": 0, "price_in_vat": 0.242,
                "start_price_in_vat": 0, "currency": "EUR", "vat_percentage": 21},
    "plug_and_charge_notification": False,
    "plug_and_charge": {"value": True, "permission": "write"},
    "led_interaction": {"value": False, "permission": "read"},
    "publish_location": {"value": False, "permission": "write"},
    "smart_charging": True,
    "smart_charging_dynamic": True,
    "activity": "available",
    "location": {"x_coord": 50.1234, "y_coord": 5.01234, "street": "Europalaan", "housenumber": "100",
                 "zipcode": "3526KS", "city": "Utrecht", "country": "NL"},
    "delayed_charging": {"value": False, "permission": "write", "start_time": "23:00", "end_time": "07:00",
                         "selected_days": [1, 2, 3, 4, 5]}
}
```

#### `get_charge_point_settings` - Get the settings of a charge point.

All the information returned by this endpoint is already included 
in the response of [`get_charge_points`](#getchargepoints---get-your-charge-points).

```python
async def get_charge_point_settings(self, evse_id: str) -> dict[str, bool | dict[str, Any] | str]
```

##### Arguments
- `evse_id`: The ID of the charge point.

##### Returns
A dictionary describing the settings:
```python
{
    "evse_id": "BCU123456",
    "plug_and_charge": {"value": True, "permission": "write"},
    "public_charging": {"value": False, "permission": "write"},
    "default_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                     "customer_name": "Your Name", "valid": 1},
    "preferred_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                       "customer_name": "Your Name", "valid": 1},
    "plug_and_charge_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                             "customer_name": "Your Name", "valid": 1},
    "smart_charging": True,
    "smart_charging_dynamic": True,
    "model_type": "H:MOVE-C32T2",
    "is_cable": True,
    "chargepoint_type": "HIDDEN",
    "plug_and_charge_notification": False,
    "led_intensity": {"value": 0, "permission": "none"},
    "led_interaction": {"value": False, "permission": "none"},
    "delayed_charging": {"value": False, "permission": "write", "start_time": "23:00", "end_time": "07:00",
                         "selected_days": [1, 2, 3, 4, 5]}
}
```

#### `get_grid_status` - Get the grid status associated to a charge point.

```python
async def get_grid_status(self, evse_id: str) -> dict[str, int | str]
```

##### Arguments
- `evse_id`: The ID of the charge point.

##### Returns
A dictionary describing the actual grid current and maximum current in amps:
```python
{
    "id": "GRID-BCU123456",
    "grid_actual_p1": 1,
    "grid_actual_p2": 2,
    "grid_actual_p3": 3,
    "grid_max_install": 25,
    "grid_max_reserved": 25
}
```

#### `get_sustainability_status` - Get statistics on the sustainability of all your charge points.

```python
async def get_sustainability_status(self) -> dict[str, float | int]
```

##### Returns
A dictionary with two keys: `{"trees": 1, "co2": 12.345}`

#### `set_plug_and_charge_charge_card` - Set the charge card for plug-and-charge

```python
async def set_plug_and_charge_charge_card(self, evse_id: str, uid: str | None = None) -> None
```

Sets the plug-and-charge card for the charge point. The uid must be a `uid` of one of your charge cards (see [`get_charge_cards`](#getchargecards---get-your-charge-cards)) or `None` to use no charge card.

#### `set_status` - Enable or disable a charge point.

```python
async def set_status(self, evse_id: str, enabled: bool) -> None
```

##### Arguments
- `evse_id`: The ID of the charge point.
- `enabled`: Boolean that indicates the desired status.

#### `get_charge_point_status` - Get the status of a charge point.

```python
async def get_charge_point_status(self, evse_id: str) -> dict[str, datetime | float | int | str | None]
```

##### Arguments
- `evse_id`: The ID of the charge point.

##### Returns
A dictionary with the chargepoint status:
```python
{
    "actual_p1": 0,
    "actual_p2": 0,
    "actual_p3": 0,
    "activity": "available",
    "actual_v1": 0,
    "actual_v2": 0,
    "actual_v3": 0,
    "actual_kwh": 0,
    "boosting": False,
    "max_usage": 20,
    "smartcharging_max_usage": 6,
    "max_offline": 10,
    "offline_since": "",
    "start_datetime": datetime(2023, 7, 24, 15, 25, 33),
    "stop_datetime": datetime(2023, 7, 26, 7, 48, 40),
    "total_cost": 9.93,
    "vehicle_status": "A",
    "evse_id": "BCU123456",
}
```

#### `set_delayed_charging` - Enable or disable delayed charging.

```python
async def set_delayed_charging(self, evse_id: str, enabled: bool) -> None
```

While delayed charging is enabled, the charge point only charges within the window configured with
[`set_delayed_charging_schedule`](#setdelayedchargingschedule---set-the-delayed-charging-schedule), and delays charging
outside of it. A charge point has at most one smart charging profile active, so enabling delayed charging disables any
other profile.

##### Arguments
- `evse_id`: The ID of the charge point.
- `enabled`: Boolean that indicates whether delayed charging should be enabled.

#### `set_delayed_charging_schedule` - Set the delayed charging schedule.

```python
async def set_delayed_charging_schedule(
    self,
    evse_id: str,
    start_time: time | str,
    end_time: time | str,
    days: Iterable[Weekday | int | str],
) -> None
```

Sets the window in which the charge point may charge on the selected days. The window may span midnight. It is only
applied while delayed charging is enabled with
[`set_delayed_charging`](#setdelayedcharging---enable-or-disable-delayed-charging).

```python
from datetime import time
from pybluecurrent import Weekday

await client.set_delayed_charging_schedule(
    "BCU123456", start_time=time(23, 0), end_time=time(7, 0), days=[Weekday.MONDAY, "tu", 3]
)
```

##### Arguments
- `evse_id`: The ID of the charge point.
- `start_time`: The time at which charging may start, as a `time` or a `"HH:MM"` string.
- `end_time`: The time at which charging must stop, as a `time` or a `"HH:MM"` string.
- `days`: The days on which the schedule applies. Each day may be a `pybluecurrent.Weekday`, an isoweekday number
  (1 for Monday through 7 for Sunday), or a weekday name such as `"monday"` or `"mo"`.

The schedule is read back from the `delayed_charging` key of
[`get_charge_point_settings`](#getchargepointsettings---get-the-settings-of-a-charge-point).

#### `boost` - Charge now, overriding the delayed charging window.

```python
async def boost(self, evse_id: str) -> None
```

Starts charging immediately, ignoring the delayed charging window for the ongoing session. The override cannot be
undone. While it is active, [`get_charge_point_status`](#getchargepointstatus---get-the-status-of-a-charge-point)
reports `"boosting": True`.

##### Arguments
- `evse_id`: The ID of the charge point.

#### `get_contracts` - Get your contracts.

```python
async def get_contracts(self) -> list[dict[str, str]]
```

##### Returns
A list of dictionaries, each representing a contract:
```python
[
    {
        "contract_id": "BCU12345678",
        "contact_email": "your@email.address",
        "subscription_type": "BASIS",
        "beneficiary_name": "Your Name",
        "iban_beneficiary": "NL00ABCD0123456789"
    }
]
```

#### `get_grids` - Get your grid connections.

```python
async def get_grids(self) -> list[dict[str, bool | dict[str, str] | str]]
```

##### Returns
A list of dictionaries, each representing a grid:
```python
[
    {
        "address": {"street": "Street Name", "housenumber": "1", "postal_code": "1234AB",
                    "city": "Amsterdam", "country": "NL", "region": ""},
        "smart_charging": True,
        "id": "GRID-BCU123456"
    }
]
```

#### `get_transactions` - Get a list of transactions.

```python
async def get_transactions(
        self, evse_id: str, newest_first: bool = True, page: int = 1
    ) -> dict[str, int | list[dict[str, Any]]]
```

##### Arguments
- `evse_id`: The ID of the charge point.
- `newest_first`: If `True`, start with the most recent transaction. Defaults to `True`.
- `page`: Page number to get. Defaults to `1`.

##### Returns
A dictionary like this:
```python
{
    "current_page": 1,
    "next_page": 2,  # This is None when there is no next page.
    "max_per_page": 25,
    "total_pages": 8,
    "transactions: [
        {
            "transaction_id": 12345678,
            "chargepoint_id": "BCU123456",
            "chargepoint_type": "HIDDEN",
            "evse_name": "Charge Point Name",
            "started_at": datetime(2023, 7, 1, 12, 34, 56),
            "end_time": datetime(2023, 7, 1, 14, 0, 0),
            "kwh": 12.34,
            "card_id": "NL-ABC-123456-0",
            "card_name": "Card Name",
            "total_costs": 5.97,
            "total_costs_ex_vat": 4.93,
            "vat": 21,
            "currency": "EUR"
        },
        ...
    ]
}
```

#### `iterate_transactions` - Iterate through your transactions

```python
async def iterate_transactions(self, evse_id: str, newest_first: bool = True) -> AsyncIterable[dict[str, Any]]
```

##### Arguments
- `evse_id`: The ID of the charge point.
- `newest_first`: If `True`, start with the most recent transaction. Defaults to `True`.

##### Returns
An iterable of dictionaries describing the transactions. Each dictionary looks like this:
```python
{
    "transaction_id": 12345678,
    "chargepoint_id": "BCU123456",
    "chargepoint_type": "HIDDEN",
    "evse_name": "Charge Point Name",
    "started_at": datetime(2023, 7, 1, 12, 34, 56),
    "end_time": datetime(2023, 7, 1, 14, 0, 0),
    "kwh": 12.34,
    "card_id": "NL-ABC-123456-0",
    "card_name": "Card Name",
    "total_costs": 5.97,
    "total_costs_ex_vat": 4.93,
    "vat": 21,
    "currency": "EUR"
}
```
