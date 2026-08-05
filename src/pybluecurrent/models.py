"""Typed response models for the BlueCurrent API.

Each getter on ``BlueCurrentClient`` returns one of these ``TypedDict``s. They are pure
annotations: the responses stay plain dictionaries, so ``response["key"]`` access, ``.get()``,
``**response`` and ``json.dumps`` all keep working, and an unexpected field the backend adds
simply rides along untouched.

Sub-shapes that recur across responses (the ``{value, permission}`` wrapper, cards, tariffs,
locations) are factored out and reused.
"""

from datetime import date, datetime, time
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
    (``selected_days``) is normalized to ``days`` before you see it. ``start_time``/``end_time``
    are parsed from the backend's "HH:MM" strings into ``time`` objects.
    """

    start_time: time
    end_time: time
    days: list[int]


class PriceBasedCharging(BoolSetting, total=False):
    """The price-based charging profile: on/off plus, when enabled, its settings.

    ``expected_departure_time`` matches ``set_price_based_charging_settings(...)``; the backend's
    own read key (``expected_leave_time``) is normalized to it before you see it, and parsed from
    the backend's "HH:MM" string into a ``time`` object.
    """

    expected_departure_time: time
    expected_kwh: float
    minimum_kwh: float


class CapacityTariff(BoolSetting, total=False):
    """The capacity-tariff setting: an on/off toggle with a maximum energy value in kWh.

    ``max_kwh`` matches ``set_capacity_tariff(max_kwh=...)``; the backend's own read key
    (``capacitytariffmaxkwh``) is normalized to it before you see it. It may be ``None`` or
    absent while the setting has never been configured.
    """

    max_kwh: float | None


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
    permission: str


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


class _ChargePointCommon(TypedDict):
    """Fields common to ``ChargePoint`` and ``ChargePointSettings``."""

    evse_id: str
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
    location: Location
    delayed_charging: DelayedCharging
    price_based_charging: PriceBasedCharging


class _ChargePointCards(TypedDict, total=False):
    """The optional plug-and-charge card field, shared by a charge point and its settings.

    ``plug_and_charge_charge_card`` is the card currently used for plug-and-charge (``None`` when none).
    """

    plug_and_charge_charge_card: CardRef | None


class _ChargePointCapacityTariff(TypedDict, total=False):
    """The optional capacity-tariff field, shared by a charge point and its settings.

    Declared optional: it is confirmed on the settings response, but not on every response
    describing a charge point.
    """

    capacity_tariff: CapacityTariff


class ChargePoint(_ChargePointCommon, _ChargePointCards, _ChargePointCapacityTariff):
    """A charge point, as returned by ``get_charge_points``.

    A disabled smart-charging profile is still present, slimmed to its ``{value, permission}``
    wrapper; its schedule/settings fields appear only while the profile is enabled.

    ``activity`` is a single unit-level value. For a multi-socket charge point (``len(socket_ids) >
    1``) it does not describe an individual socket; use ``get_charge_point_statuses`` for the
    authoritative per-socket status.
    """

    name: str
    activity: str
    socket_ids: list[int]  # the sockets this charge point has (one for most, two for dual-socket models)


class ChargePointSettings(_ChargePointCommon, _ChargePointCards, _ChargePointCapacityTariff):
    """The settings of a charge point, as returned by ``get_charge_point_settings``.

    A disabled smart-charging profile is still present, slimmed to its ``{value, permission}``
    wrapper; its schedule/settings fields appear only while the profile is enabled.
    """

    chargepoint_name: str
    led_intensity: IntSetting


class GridStatus(TypedDict):
    """The grid status of a charge point, as returned by ``get_grid_status`` (currents in amps)."""

    id: str
    grid_actual_p1: float  # actual current on grid phase L1
    grid_actual_p2: float  # actual current on grid phase L2
    grid_actual_p3: float  # actual current on grid phase L3
    grid_max_install: float  # the grid connection's installed maximum current (per phase)
    grid_max_reserved: float  # maximum grid current the charge point(s) may use together


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
    max_usage: float  # maximum current the charge point can deliver, in amps
    smartcharging_max_usage: float  # the smart-charging current limit in effect right now, in amps (updates live)
    max_offline: float  # maximum charging current, in amps, while the charge point is offline
    offline_since: str
    start_datetime: datetime | None
    stop_datetime: datetime | None
    total_cost: float
    vehicle_status: str
    evse_id: str
    socket_id: int


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


class _TransactionBase(TypedDict):
    transaction_id: int
    chargepoint_id: str
    socket_id: int
    chargepoint_type: str
    evse_name: str
    started_at: datetime | None
    end_time: datetime | None
    kwh: float
    card_id: str
    card_name: str
    total_costs: float
    total_costs_ex_vat: float
    reimbursement_tariff_ex_vat: float
    vat: int
    currency: str


class Transaction(_TransactionBase, total=False):
    """A single charging transaction.

    ``reason_no_settlement`` holds the reason a transaction was not settled, as a string. A
    normally settled transaction has no reason: the key is either absent or present as ``None`` —
    so treat both a missing key and ``None`` as "settled".
    """

    reason_no_settlement: str | None


class TransactionsPage(TypedDict):
    """A page of transactions, as returned by ``get_transactions``."""

    current_page: int
    next_page: int | None
    max_per_page: int
    total_pages: int
    total_transactions: int
    transactions: list[Transaction]
