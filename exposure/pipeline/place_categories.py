"""Map Overture `place` categories -> 23 exposure place classes, count per cell.

Overture tags each place with `categories.primary`, a fine-grained string from
an 885+ value taxonomy (e.g. `pizza_restaurant`, `catholic_church`,
`childrens_hospital`). We fold those into the 23 OSM-style classes used by the
IBF exposure work (atm, bakery, bank, ... trainstation) via a small set of
exact matches plus suffix rules that absorb the many sub-variants.

`classify(primary)` -> class name (one of CLASSES) or None (place is still
counted in `place_count`, just not in any class column).
"""
from __future__ import annotations

# 23 target classes, in the order requested (= column order pl_<class>).
CLASSES = [
    "atm", "bakery", "bank", "bar", "bus_station", "cafe", "church",
    "cloth_store", "convenience_store", "department_store", "funeralhome",
    "gas_station", "hospital", "lodging", "mosque", "movie_theater", "parking",
    "temple", "restaurant", "shopping_mall", "super_market", "taxi_stand",
    "trainstation",
]

# Exact Overture primary-category -> class (verified against the live taxonomy).
EXACT = {
    "atms": "atm",
    "bakery": "bakery",
    "banks": "bank", "bank_credit_union": "bank",
    "bus_station": "bus_station",
    "cafe": "cafe", "coffee_shop": "cafe", "internet_cafe": "cafe",
    "church_cathedral": "church",
    "convenience_store": "convenience_store",
    "department_store": "department_store",
    "funeral_services_and_cemeteries": "funeralhome",
    "gas_station": "gas_station", "truck_gas_station": "gas_station",
    "hospital": "hospital", "childrens_hospital": "hospital",
    "hotel": "lodging", "motel": "lodging", "hostel": "lodging",
    "lodging": "lodging", "resort": "lodging", "guest_house": "lodging",
    "bed_and_breakfast": "lodging",
    "mosque": "mosque",
    "cinema": "movie_theater", "movie_theater": "movie_theater",
    "parking": "parking",
    "shopping_center": "shopping_mall", "shopping_mall": "shopping_mall",
    "supermarket": "super_market", "grocery_store": "super_market",
    "taxi_service": "taxi_stand",
    "train_station": "trainstation",
}

# Overture strings that contain a rule keyword but must NOT match it.
_NOT_RESTAURANT = {"restaurant_equipment_and_supply", "restaurant_wholesale"}


def classify(primary: str | None) -> str | None:
    """Fold one Overture primary category into a target class (or None)."""
    if not primary:
        return None
    if primary in EXACT:
        return EXACT[primary]
    # suffix rules for the many granular sub-variants
    if (primary == "restaurant" or primary.endswith("_restaurant")) \
            and primary not in _NOT_RESTAURANT:
        return "restaurant"
    if primary == "bar" or primary.endswith("_bar"):
        return "bar"
    if primary.endswith("_church"):
        return "church"
    if primary.endswith("_temple"):
        return "temple"
    if primary == "clothing_store" or primary.endswith("_clothing_store"):
        return "cloth_store"
    return None


def col(cls: str) -> str:
    """Per-cell column name for a class count."""
    return f"pl_{cls}"


PLACE_COLS = [col(c) for c in CLASSES]
