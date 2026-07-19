"""Typed response models for the BlueCurrent API.

Each getter on ``BlueCurrentClient`` returns one of these ``TypedDict``s. They are pure
annotations: the responses stay plain dictionaries, so ``response["key"]`` access, ``.get()``,
``**response`` and ``json.dumps`` all keep working, and an unexpected field the backend adds
simply rides along untouched.

Sub-shapes that recur across responses (the ``{value, permission}`` wrapper, cards, tariffs,
locations) are factored out and reused.
"""

from datetime import date, datetime
from typing import TypedDict


class BoolSetting(TypedDict):
    """A boolean setting together with the caller's permission to change it."""

    value: bool
    permission: str


class IntSetting(TypedDict):
    """An integer setting together with the caller's permission to change it."""

    value: int
    permission: str


class DelayedCharging(BoolSetting, total=False):
    """The delayed-charging profile: on/off plus, when configured, its window and days.

    ``days`` are isoweekday numbers (1 for Monday through 7 for Sunday). This is the same
    vocabulary as ``set_delayed_charging_schedule(days=...)``; the backend's own read key
    (``selected_days``) is normalized to ``days`` before you see it.
    """

    start_time: str
    end_time: str
    days: list[int]


class PriceBasedCharging(BoolSetting, total=False):
    """The price-based charging profile: on/off plus, when enabled, its settings.

    ``expected_departure_time`` matches ``set_price_based_charging_settings(...)``; the backend's
    own read key (``expected_leave_time``) is normalized to it before you see it.
    """

    expected_departure_time: str
    expected_kwh: float
    minimum_kwh: float


class _CardBase(TypedDict):
    uid: str
    id: str
    customer_name: str


class CardRef(_CardBase, total=False):
    """A charge card as embedded in a charge point's settings.

    ``name`` and ``valid`` are absent for the built-in ``BCU-APP`` pseudo-card.
    """

    name: str
    valid: int


class ChargeCard(CardRef):
    """A charge card as returned by ``get_charge_cards``, with its validity dates."""

    date_created: date | None
    date_modified: date | None
    date_became_invalid: date | None


class _TariffBase(TypedDict):
    tariff_id: str
    price_ex_vat: float
    start_price_ex_vat: float
    price_in_vat: float
    start_price_in_vat: float
    currency: str


class Tariff(_TariffBase, total=False):
    """The tariff applied at a charge point."""

    vat_percentage: int


class Location(TypedDict):
    """A charge point's geographic location."""

    x_coord: float
    y_coord: float
    street: str
    housenumber: str
    zipcode: str
    city: str
    country: str


class Address(TypedDict):
    """A grid connection's address."""

    street: str
    housenumber: str
    postal_code: str
    city: str
    country: str
    region: str


class Account(TypedDict):
    """Your account, as returned by ``get_account``."""

    full_name: str
    email: str | None
    login: str
    should_reset_password: bool
    developer_mode_enabled: bool
    tel: str | None
    marketing_target: str
    first_login_app: datetime
    hubspot_user_identity: str


class _ChargePointBase(TypedDict):
    evse_id: str
    name: str
    model_type: str
    chargepoint_type: str
    is_cable: bool
    public_charging: BoolSetting
    default_card: CardRef
    preferred_card: CardRef
    tariff: Tariff
    plug_and_charge_notification: BoolSetting
    plug_and_charge: BoolSetting
    led_interaction: BoolSetting
    publish_location: BoolSetting
    smart_charging: bool
    smart_charging_dynamic: bool
    activity: str
    location: Location
    delayed_charging: DelayedCharging
    price_based_charging: PriceBasedCharging


class ChargePoint(_ChargePointBase, total=False):
    """A charge point, as returned by ``get_charge_points``.

    A disabled smart-charging profile is still present, slimmed to its ``{value, permission}``
    wrapper; its schedule/settings fields appear only while the profile is enabled.
    ``plug_and_charge_card`` is absent when no plug-and-charge card is configured.
    """

    plug_and_charge_card: CardRef


class _ChargePointSettingsBase(TypedDict):
    evse_id: str
    plug_and_charge: BoolSetting
    public_charging: BoolSetting
    default_card: CardRef
    preferred_card: CardRef
    smart_charging: bool
    smart_charging_dynamic: bool
    model_type: str
    is_cable: bool
    chargepoint_type: str
    plug_and_charge_notification: BoolSetting
    led_intensity: IntSetting
    led_interaction: BoolSetting
    delayed_charging: DelayedCharging
    price_based_charging: PriceBasedCharging


class ChargePointSettings(_ChargePointSettingsBase, total=False):
    """The settings of a charge point, as returned by ``get_charge_point_settings``.

    A disabled smart-charging profile is still present, slimmed to its ``{value, permission}``
    wrapper; its schedule/settings fields appear only while the profile is enabled.
    ``plug_and_charge_card`` is absent when no plug-and-charge card is configured.
    """

    plug_and_charge_card: CardRef


class GridStatus(TypedDict):
    """The grid status of a charge point, as returned by ``get_grid_status`` (currents in amps)."""

    id: str
    grid_actual_p1: float
    grid_actual_p2: float
    grid_actual_p3: float
    grid_max_install: float
    grid_max_reserved: float


class SustainabilityStatus(TypedDict):
    """Sustainability statistics, as returned by ``get_sustainability_status``."""

    trees: int
    co2: float


class ChargePointStatus(TypedDict):
    """The live status of a charge point, as returned by ``get_charge_point_status``."""

    actual_p1: float
    actual_p2: float
    actual_p3: float
    activity: str
    actual_v1: float
    actual_v2: float
    actual_v3: float
    actual_kwh: float
    boosting: bool
    max_usage: float
    smartcharging_max_usage: float
    max_offline: float
    offline_since: str
    start_datetime: datetime | None
    stop_datetime: datetime | None
    total_cost: float
    vehicle_status: str
    evse_id: str


class Contract(TypedDict):
    """A contract, as returned by ``get_contracts``."""

    contract_id: str
    contact_email: str
    subscription_type: str
    beneficiary_name: str
    iban_beneficiary: str


class Grid(TypedDict):
    """A grid connection, as returned by ``get_grids``."""

    address: Address
    smart_charging: bool
    id: str


class Transaction(TypedDict):
    """A single charging transaction."""

    transaction_id: int
    chargepoint_id: str
    chargepoint_type: str
    evse_name: str
    started_at: datetime | None
    end_time: datetime | None
    kwh: float
    card_id: str
    card_name: str
    total_costs: float
    total_costs_ex_vat: float
    vat: int
    currency: str


class TransactionsPage(TypedDict):
    """A page of transactions, as returned by ``get_transactions``."""

    current_page: int
    next_page: int | None
    max_per_page: int
    total_pages: int
    transactions: list[Transaction]
