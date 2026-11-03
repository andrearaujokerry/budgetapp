"""The demo persona: one Amex Blue Cash Preferred card in San Francisco and where it is used.

Every merchant carries what Plaid would send for it (``name``, ``merchant_name``, the raw
statement text, PFC v2 codes, channel, MCC, website, a store location) and the Amex CSV
"Category" the extended download gives it. ``category`` is the app category the real
``services.categorize.Categorizer`` gives a Plaid row of that merchant (through the seeded PFC
mapping or one of :data:`RULES`); the planner uses it only to shape the current month, and the
writer always stores what the Categorizer decides.

All names, places and people are made up or public businesses; nothing here is real account data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

CARD_NAME = "Blue Cash Preferred®"
CARD_OFFICIAL_NAME = "Blue Cash Preferred® Card"
CARD_MASK = "3005"
CREDIT_LIMIT = Decimal("18000.00")
CLOSING_DAY = 27
APR = Decimal("0.2124")
ANNUAL_FEE = Decimal("95.00")
FX_FEE_RATE = Decimal("0.027")
INSTITUTION_ID = "ins_10"
INSTITUTION_NAME = "American Express"
CARD_MEMBER = "ALEX RIVERA"
ADDITIONAL_CARD_MEMBER = "SAM RIVERA"
#: The one category the persona added: (name, colour, icon).
CUSTOM_CATEGORY = ("Pets", "#9f1239", "🐾")


@dataclass(frozen=True, slots=True)
class Place:
    """A store location shaped like Plaid's ``location`` object."""

    address: str
    city: str
    region: str | None
    postal_code: str
    country: str
    lat: str
    lon: str
    store_number: str | None = None


@dataclass(frozen=True, slots=True)
class Merchant:
    """One merchant as Plaid and the Amex CSV describe it.

    ``name`` and ``raw`` may contain ``{v}`` (a per-transaction variant from ``variants``, e.g. a
    DoorDash restaurant) and ``{code}`` (a random order or ticket code). ``lo``/``hi`` are the
    usual amount range in cents.
    """

    key: str
    merchant_name: str | None
    name: str
    raw: str
    category: str
    pfc: tuple[str, str] | None
    amex: str
    lo: int
    hi: int
    channel: str = "in store"
    mcc: str | None = None
    website: str | None = None
    place: Place | None = None
    confidence: str = "VERY_HIGH"
    foreign: bool = False
    variants: tuple[str, ...] = ()


PFC_PRIMARIES = (
    "FOOD_AND_DRINK",
    "GENERAL_MERCHANDISE",
    "GENERAL_SERVICES",
    "GOVERNMENT_AND_NON_PROFIT",
    "TRANSPORTATION",
    "TRAVEL",
    "ENTERTAINMENT",
    "PERSONAL_CARE",
    "MEDICAL",
    "HOME_IMPROVEMENT",
    "RENT_AND_UTILITIES",
    "TRANSFER_OUT",
    "TRANSFER_IN",
    "BANK_FEES",
    "LOAN_PAYMENTS",
    "INCOME",
)

GROCERIES = "Merchandise & Supplies-Groceries"
RESTAURANT = "Restaurant-Restaurant"
CAFE = "Restaurant-Bar & Café"
INTERNET = "Merchandise & Supplies-Internet Purchase"
FEES_ADJ = "Fees & Adjustments-Fees & Adjustments"


def _sf(address: str, postal: str, lat: str, lon: str, store: str | None = None) -> Place:
    return Place(address, "San Francisco", "CA", postal, "US", lat, lon, store)


def _at(city: str, region: str | None, address: str, postal: str, lat: str, lon: str) -> Place:
    """A place away from home (``region=None`` is Lisbon, Portugal)."""
    return Place(address, city, region, postal, "PT" if region is None else "US", lat, lon)


def _m(
    key: str,
    merchant_name: str | None,
    name: str,
    raw: str,
    category: str,
    pfc: str | None,
    amex: str,
    lo: str,
    hi: str,
    **options: object,
) -> Merchant:
    """Shorthand: ``pfc`` is the detailed code (its primary is derived), amounts are dollars."""
    codes = None
    if pfc is not None:
        codes = (next(p for p in PFC_PRIMARIES if pfc.startswith(p + "_")), pfc)
    low, high = int(Decimal(lo) * 100), int(Decimal(hi) * 100)
    return Merchant(
        key,
        merchant_name,
        name,
        raw,
        category,
        codes,
        amex,
        low,
        high,
        **options,  # type: ignore[arg-type]
    )


# Columns: key, merchant_name, Plaid name, raw statement text, app category, PFC v2 detailed code,
# Amex CSV category, usual low and high amount, then options.
# fmt: off
_MERCHANTS = [
    # ------------------------------------------------------------------ groceries
    _m("whole_foods", "Whole Foods Market", "WHOLEFDS MKT 10234",
       "WHOLEFDS MKT 10234 SAN FRANCISCO CA", "Groceries", "FOOD_AND_DRINK_GROCERIES",
       GROCERIES, "18.40", "142.00", mcc="5411", website="wholefoodsmarket.com",
       place=_sf("2001 Market St", "94114", "37.7678", "-122.4296", "10234")),
    _m("trader_joes", "Trader Joe's", "TRADER JOE S #236",
       "TRADER JOE S #236 QPS SAN FRANCISCO CA", "Groceries", "FOOD_AND_DRINK_GROCERIES",
       GROCERIES, "21.00", "96.00", mcc="5411", website="traderjoes.com",
       place=_sf("555 9th St", "94103", "37.7705", "-122.4075", "236")),
    _m("safeway", "Safeway", "SAFEWAY #1490", "SAFEWAY #1490 SAN FRANCISCO CA", "Groceries",
       "FOOD_AND_DRINK_GROCERIES", GROCERIES, "11.50", "84.00", mcc="5411",
       website="safeway.com", place=_sf("2020 Market St", "94114", "37.7688", "-122.4278", "1490")),
    _m("bi_rite", "Bi-Rite Market", "BI-RITE MARKET", "BI-RITE MARKET SAN FRANCISCO CA",
       "Groceries", "FOOD_AND_DRINK_GROCERIES", GROCERIES, "9.25", "64.00", mcc="5411",
       website="biritemarket.com", confidence="HIGH",
       place=_sf("3639 18th St", "94110", "37.7616", "-122.4256")),
    _m("mission_market", "Mission Market", "MISSION MARKET", "MISSION MARKET SAN FRANCISCO CA",
       "Groceries", "GENERAL_MERCHANDISE_CONVENIENCE_STORES", GROCERIES, "3.49", "18.00",
       mcc="5499", confidence="MEDIUM",
       place=_sf("2590 Mission St", "94110", "37.7564", "-122.4186")),
    _m("costco", "Costco", "COSTCO WHSE #0144", "COSTCO WHSE #0144 SAN FRANCISCO CA",
       "Groceries", "GENERAL_MERCHANDISE_SUPERSTORES", "Merchandise & Supplies-Wholesale Stores",
       "96.00", "268.00", mcc="5300", website="costco.com",
       place=_sf("450 10th St", "94103", "37.7703", "-122.4110", "144")),
    # ------------------------------------------------------------------ dining & drinks
    _m("starbucks", "Starbucks", "STARBUCKS STORE 05823",
       "STARBUCKS STORE 05823 SAN FRANCISCO CA", "Dining & Drinks", "FOOD_AND_DRINK_COFFEE",
       CAFE, "4.45", "9.85", mcc="5814", website="starbucks.com",
       place=_sf("3727 24th St", "94114", "37.7516", "-122.4302", "05823")),
    _m("blue_bottle", "Blue Bottle Coffee", "BLUE BOTTLE COFFEE",
       "BLUE BOTTLE COFFEE SAN FRANCISCO CA", "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE,
       "5.50", "12.75", mcc="5814", website="bluebottlecoffee.com",
       place=_sf("66 Mint St", "94103", "37.7825", "-122.4071")),
    _m("philz", "Philz Coffee", "PHILZ COFFEE 24TH", "PHILZ COFFEE 24TH SAN FRANCISCO CA",
       "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE, "5.25", "11.40", mcc="5814",
       website="philzcoffee.com", place=_sf("3101 24th St", "94110", "37.7526", "-122.4146")),
    _m("arizmendi", "Arizmendi Bakery", "ARIZMENDI BAKERY", "ARIZMENDI BAKERY SAN FRANCISCO CA",
       "Dining & Drinks", "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", CAFE, "3.75", "12.50",
       mcc="5462", confidence="HIGH",
       place=_sf("1268 Valencia St", "94110", "37.7523", "-122.4203")),
    _m("tartine", "Tartine Bakery", "TARTINE BAKERY", "TST* TARTINE BAKERY SAN FRANCISCO CA",
       "Dining & Drinks", "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", CAFE, "7.50", "34.00",
       mcc="5462", website="tartinebakery.com", confidence="HIGH",
       place=_sf("600 Guerrero St", "94110", "37.7614", "-122.4241")),
    _m("sweetgreen", "Sweetgreen", "SWEETGREEN 2ND ST", "SWEETGREEN 2ND ST SAN FRANCISCO CA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "13.45", "19.80",
       mcc="5814", website="sweetgreen.com",
       place=_sf("171 2nd St", "94105", "37.7865", "-122.3990")),
    _m("chipotle", "Chipotle Mexican Grill", "CHIPOTLE 1093", "CHIPOTLE 1093 SAN FRANCISCO CA",
       "Dining & Drinks", "FOOD_AND_DRINK_FAST_FOOD", RESTAURANT, "11.20", "17.60", mcc="5814",
       website="chipotle.com",
       place=_sf("211 Sutter St", "94108", "37.7896", "-122.4040", "1093")),
    _m("souvla", "Souvla", "SOUVLA HAYES", "TST* SOUVLA HAYES SAN FRANCISCO CA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "18.00", "46.00",
       mcc="5812", website="souvla.com", confidence="HIGH",
       place=_sf("517 Hayes St", "94102", "37.7765", "-122.4253")),
    _m("la_taqueria", "La Taqueria", "LA TAQUERIA", "LA TAQUERIA SAN FRANCISCO CA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "12.50", "34.00",
       mcc="5812", confidence="HIGH",
       place=_sf("2889 Mission St", "94110", "37.7509", "-122.4181")),
    _m("nopa", "Nopa", "NOPA", "NOPA SAN FRANCISCO CA", "Dining & Drinks",
       "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "68.00", "184.00", mcc="5812",
       website="nopasf.com", confidence="HIGH",
       place=_sf("560 Divisadero St", "94117", "37.7749", "-122.4376")),
    _m("zuni", "Zuni Café", "ZUNI CAFE", "ZUNI CAFE SAN FRANCISCO CA", "Dining & Drinks",
       "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "74.00", "212.00", mcc="5812",
       website="zunicafe.com", confidence="HIGH",
       place=_sf("1658 Market St", "94102", "37.7736", "-122.4216")),
    _m("delfina", "Delfina", "DELFINA", "SQ *DELFINA SAN FRANCISCO CA", "Dining & Drinks",
       "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "58.00", "156.00", mcc="5812",
       website="delfinasf.com", confidence="HIGH",
       place=_sf("3621 18th St", "94110", "37.7614", "-122.4243")),
    _m("zeitgeist", "Zeitgeist", "ZEITGEIST", "ZEITGEIST SAN FRANCISCO CA", "Dining & Drinks",
       "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR", CAFE, "14.00", "58.00", mcc="5813",
       confidence="HIGH", place=_sf("199 Valencia St", "94103", "37.7701", "-122.4222")),
    _m("doordash", "DoorDash", "DOORDASH*{v}", "DD *DOORDASH {v} 855-973-1040 CA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "23.80", "61.00",
       channel="online", mcc="5812", website="doordash.com",
       variants=("BURMA LOVE", "THAI HOUSE", "PIZZERIA DELFINA", "SUSHI ZONE", "SOUVLA")),
    _m("uber_eats", "Uber Eats", "UBER EATS", "UBER *EATS HELP.UBER.COM CA", "Dining & Drinks",
       "FOOD_AND_DRINK_FAST_FOOD", RESTAURANT, "21.50", "49.00", channel="online", mcc="5812",
       website="ubereats.com"),
    # ------------------------------------------------------------------ transportation
    _m("uber", "Uber", "UBER *TRIP", "UBER *TRIP HELP.UBER.COM CA", "Transportation",
       "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "Transportation-Taxis & Coach", "8.90", "46.00",
       channel="online", mcc="4121", website="uber.com"),
    _m("lyft", "Lyft", "LYFT *RIDE", "LYFT *RIDE SAN FRANCISCO CA", "Transportation",
       "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "Transportation-Taxis & Coach", "8.10", "41.00",
       channel="online", mcc="4121", website="lyft.com"),
    _m("clipper", "Clipper", "CLIPPER SERVICE", "CLIPPER SERVICE 877-878-8883 CA",
       "Transportation", "TRANSPORTATION_PUBLIC_TRANSIT", "Transportation-Rail Services",
       "40.00", "50.00", channel="online", mcc="4111", website="clippercard.com"),
    _m("chevron", "Chevron", "CHEVRON 0091542", "CHEVRON 0091542 SAN FRANCISCO CA",
       "Transportation", "TRANSPORTATION_GAS", "Transportation-Fuel", "41.00", "72.00",
       mcc="5541", website="chevron.com",
       place=_sf("1400 Folsom St", "94103", "37.7727", "-122.4128", "0091542")),
    _m("bay_wheels", "Bay Wheels", "LYFT *BAY WHEELS", "LYFT *BAY WHEELS SAN FRANCISCO CA",
       "Transportation", "TRANSPORTATION_BIKES_AND_SCOOTERS", "Transportation-Taxis & Coach",
       "3.49", "8.20", channel="online", mcc="4121", website="baywheels.com"),
    _m("paybyphone", "SFMTA", "SFMTA PAYBYPHONE", "SFMTA PAYBYPHONE SAN FRANCISCO CA",
       "Transportation", "TRANSPORTATION_PARKING", "Transportation-Parking Charges", "2.50",
       "18.00", channel="online", mcc="7523", website="sfmta.com", confidence="HIGH"),
    _m("fastrak", "FasTrak", "FASTRAK CSC", "FASTRAK CSC 877-229-8655 CA", "Transportation",
       "TRANSPORTATION_TOLLS", "Transportation-Tolls & Fees", "25.00", "25.00",
       channel="online", mcc="4784", website="bayareafastrak.org", confidence="HIGH"),
    # ------------------------------------------------------------------ shopping
    _m("amazon", "Amazon", "AMAZON MKTPL*{code}", "AMAZON MKTPL*{code} AMZN.COM/BILL WA",
       "Shopping", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", INTERNET, "8.99", "120.00",
       channel="online", mcc="5999", website="amazon.com"),
    _m("kindle", "Amazon Kindle", "KINDLE SVCS*{code}", "KINDLE SVCS*{code} 888-802-3080 WA",
       "Shopping", "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS", INTERNET, "2.99", "14.99",
       channel="online", mcc="5815", website="amazon.com"),
    _m("target", "Target", "TARGET 00027409", "TARGET 00027409 SAN FRANCISCO CA", "Shopping",
       "GENERAL_MERCHANDISE_SUPERSTORES", "Merchandise & Supplies-Department Stores", "14.00",
       "138.00", mcc="5310", website="target.com",
       place=_sf("789 Mission St", "94103", "37.7845", "-122.4045", "2740")),
    _m("uniqlo", "Uniqlo", "UNIQLO USA POWELL", "UNIQLO USA POWELL ST SAN FRANCISCO CA",
       "Shopping", "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES",
       "Merchandise & Supplies-Clothing Stores", "29.90", "149.00", mcc="5651",
       website="uniqlo.com", place=_sf("111 Powell St", "94102", "37.7858", "-122.4079")),
    _m("rei", "REI", "REI #87", "REI #87 SAN FRANCISCO CA", "Shopping",
       "GENERAL_MERCHANDISE_SPORTING_GOODS", "Merchandise & Supplies-Sporting Goods Stores",
       "24.95", "264.00", mcc="5941", website="rei.com",
       place=_sf("840 Brannan St", "94103", "37.7714", "-122.4049", "87")),
    _m("apple_store", "Apple", "APPLE STORE #R075", "APPLE STORE #R075 SAN FRANCISCO CA",
       "Shopping", "GENERAL_MERCHANDISE_ELECTRONICS", "Merchandise & Supplies-Computer Supplies",
       "29.00", "1299.00", mcc="5732", website="apple.com",
       place=_sf("300 Post St", "94108", "37.7885", "-122.4075", "R075")),
    _m("green_apple", "Green Apple Books", "GREEN APPLE BOOKS",
       "GREEN APPLE BOOKS SAN FRANCISCO CA", "Shopping",
       "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS", "Merchandise & Supplies-Book Stores",
       "12.00", "48.00", mcc="5942", website="greenapplebooks.com", confidence="HIGH",
       place=_sf("506 Clement St", "94118", "37.7830", "-122.4645")),
    _m("best_buy", "Best Buy", "BEST BUY 00001834", "BEST BUY 00001834 SAN FRANCISCO CA",
       "Shopping", "GENERAL_MERCHANDISE_ELECTRONICS", "Merchandise & Supplies-Electronics Stores",
       "24.99", "219.00", mcc="5732", website="bestbuy.com",
       place=_sf("1717 Harrison St", "94103", "37.7692", "-122.4133", "1834")),
    _m("etsy", "Etsy", "ETSY.COM - {v}", "ETSY.COM - {v} BROOKLYN NY", "Shopping",
       "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", INTERNET, "18.00", "86.00", channel="online",
       mcc="5999", website="etsy.com",
       variants=("CERAMICSBYLENA", "PRINTSHOPSF", "WOVENGOODSCO", "THEPAPERNOOK")),
    # ------------------------------------------------------------------ entertainment
    _m("ticketmaster", "Ticketmaster", "TICKETMASTER", "TM *TICKETMASTER 800-653-8000 CA",
       "Entertainment", "ENTERTAINMENT_OTHER_ENTERTAINMENT", "Entertainment-Theatrical Events",
       "48.00", "264.00", channel="online", mcc="7922", website="ticketmaster.com"),
    _m("giants", "San Francisco Giants", "SF GIANTS TICKETS",
       "SAN FRANCISCO GIANTS SAN FRANCISCO CA", "Entertainment",
       "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
       "Entertainment-Sporting Events", "32.00", "96.00", mcc="7941", website="mlb.com",
       place=_sf("24 Willie Mays Plaza", "94107", "37.7786", "-122.3893")),
    _m("sfmoma", "SFMOMA", "SFMOMA", "SFMOMA SAN FRANCISCO CA", "Entertainment",
       "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
       "Entertainment-Other Entertainment", "25.00", "60.00", mcc="7991", website="sfmoma.org",
       place=_sf("151 3rd St", "94103", "37.7857", "-122.4011")),
    _m("steam", "Steam", "STEAMGAMES.COM", "STEAMGAMES.COM 4259522985 WA", "Entertainment",
       "ENTERTAINMENT_VIDEO_GAMES", INTERNET, "4.99", "59.99", channel="online", mcc="5816",
       website="steampowered.com"),
    _m("bowling", "Mission Bowling Club", "MISSION BOWLING CLUB",
       "TST* MISSION BOWLING CLUB SAN FRANCISCO CA", "Entertainment",
       "ENTERTAINMENT_OTHER_ENTERTAINMENT", "Entertainment-Sports & Recreation", "38.00",
       "92.00", mcc="7933", confidence="HIGH",
       place=_sf("3176 17th St", "94110", "37.7636", "-122.4180")),
    # ------------------------------------------------------------------ subscriptions (rules)
    _m("netflix", "Netflix", "NETFLIX.COM", "NETFLIX.COM LOS GATOS CA",
       "Subscriptions & Streaming", "ENTERTAINMENT_TV_AND_MOVIES",
       "Communications-Cable & Internet Comm", "17.99", "19.99", channel="online", mcc="4899",
       website="netflix.com"),
    _m("spotify", "Spotify", "SPOTIFY USA", "SPOTIFY USA NEW YORK NY",
       "Subscriptions & Streaming", "ENTERTAINMENT_MUSIC_AND_AUDIO", INTERNET, "11.99", "12.99",
       channel="online", mcc="5815", website="spotify.com"),
    _m("icloud", "Apple", "APPLE.COM/BILL", "APPLE.COM/BILL 866-712-7753 CA",
       "Subscriptions & Streaming", "GENERAL_MERCHANDISE_ELECTRONICS", INTERNET, "2.99", "2.99",
       channel="online", mcc="5818", website="apple.com"),
    _m("openai", "OpenAI", "OPENAI *CHATGPT SUBSCR", "OPENAI *CHATGPT SUBSCR SAN FRANCISCO CA",
       "Subscriptions & Streaming", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
       "Business Services-Other Services", "20.00", "20.00", channel="online", mcc="5734",
       website="openai.com"),
    _m("nytimes", "The New York Times", "NYTIMES*NYTDIGITAL", "NYTIMES*NYTDIGITAL NEW YORK NY",
       "Subscriptions & Streaming", "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS", INTERNET,
       "17.00", "17.00", channel="online", mcc="5994", website="nytimes.com"),
    _m("prime", "Amazon Prime", "AMAZON PRIME*{code}", "AMAZON PRIME*{code} AMZN.COM/BILL WA",
       "Subscriptions & Streaming", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", INTERNET,
       "14.99", "14.99", channel="online", mcc="5968", website="amazon.com"),
    # ------------------------------------------------------------------ health & fitness
    _m("equinox", "Equinox", "EQUINOX #142", "EQUINOX #142 SAN FRANCISCO CA",
       "Health & Fitness", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "Other-Miscellaneous",
       "245.00", "255.00", mcc="7997", website="equinox.com",
       place=_sf("747 Market St", "94103", "37.7867", "-122.4040", "142")),
    _m("cvs", "CVS Pharmacy", "CVS/PHARMACY #09732", "CVS/PHARMACY #09732 SAN FRANCISCO CA",
       "Health & Fitness", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS",
       "Merchandise & Supplies-Pharmacies", "6.49", "54.00", mcc="5912", website="cvs.com",
       place=_sf("731 Market St", "94103", "37.7866", "-122.4046", "09732")),
    _m("walgreens", "Walgreens", "WALGREENS #3297", "WALGREENS #3297 SAN FRANCISCO CA",
       "Health & Fitness", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS",
       "Merchandise & Supplies-Pharmacies", "5.99", "48.00", mcc="5912",
       website="walgreens.com",
       place=_sf("4089 24th St", "94114", "37.7512", "-122.4344", "3297")),
    _m("dentist", "Mission Dental Group", "MISSION DENTAL GROUP",
       "MISSION DENTAL GROUP SAN FRANCISCO CA", "Health & Fitness", "MEDICAL_DENTAL_CARE",
       "Business Services-Health Care Services", "95.00", "240.00", mcc="8021",
       confidence="HIGH", place=_sf("2480 Mission St", "94110", "37.7571", "-122.4189")),
    _m("one_medical", "One Medical", "ONE MEDICAL MEMBERSHIP",
       "1LIFE HEALTHCARE ONEMEDICAL.COM CA", "Health & Fitness", "MEDICAL_PRIMARY_CARE",
       "Business Services-Health Care Services", "199.00", "199.00", channel="online",
       mcc="8011", website="onemedical.com"),
    # ------------------------------------------------------------------ personal care
    _m("barber", "Peoples Barber & Shop", "PEOPLES BARBER",
       "SQ *PEOPLES BARBER & SHOP SAN FRANCISCO CA", "Personal Care",
       "PERSONAL_CARE_HAIR_AND_BEAUTY", "Other-Miscellaneous", "58.00", "68.00", mcc="7241",
       confidence="HIGH", place=_sf("3258 22nd St", "94110", "37.7553", "-122.4191")),
    _m("sephora", "Sephora", "SEPHORA #0312", "SEPHORA #0312 SAN FRANCISCO CA", "Personal Care",
       "PERSONAL_CARE_HAIR_AND_BEAUTY", "Merchandise & Supplies-Department Stores", "18.00",
       "86.00", mcc="5977", website="sephora.com",
       place=_sf("33 Powell St", "94102", "37.7849", "-122.4078", "0312")),
    _m("cleaners", "Mission Cleaners", "MISSION CLEANERS", "MISSION CLEANERS SAN FRANCISCO CA",
       "Personal Care", "PERSONAL_CARE_LAUNDRY_AND_DRY_CLEANING",
       "Business Services-Other Services", "12.00", "38.00", mcc="7216", confidence="MEDIUM",
       place=_sf("3600 20th St", "94110", "37.7587", "-122.4232")),
    # ------------------------------------------------------------------ home
    _m("home_depot", "The Home Depot", "THE HOME DEPOT #1861",
       "THE HOME DEPOT #1861 DALY CITY CA", "Home", "HOME_IMPROVEMENT_HARDWARE",
       "Merchandise & Supplies-Hardware Supplies", "12.00", "184.00", mcc="5200",
       website="homedepot.com",
       place=Place("2675 Geneva Ave", "Daly City", "CA", "94014", "US", "37.7072", "-122.4167",
                   "1861")),
    _m("cole_hardware", "Cole Hardware", "COLE HARDWARE", "COLE HARDWARE SAN FRANCISCO CA",
       "Home", "HOME_IMPROVEMENT_HARDWARE", "Merchandise & Supplies-Hardware Supplies", "7.50",
       "62.00", mcc="5251", website="colehardware.com", confidence="HIGH",
       place=_sf("956 Valencia St", "94110", "37.7570", "-122.4215")),
    _m("ikea", "IKEA", "IKEA EMERYVILLE", "IKEA EMERYVILLE EMERYVILLE CA", "Home",
       "HOME_IMPROVEMENT_FURNITURE", "Merchandise & Supplies-Furniture Stores", "180.00",
       "520.00", mcc="5712", website="ikea.com",
       place=Place("4400 Shellmound St", "Emeryville", "CA", "94608", "US", "37.8317",
                   "-122.2915", "165")),
    # ------------------------------------------------------------------ utilities & bills
    _m("verizon", "Verizon", "VERIZON WIRELESS", "VZWRLSS*APOCC VISB 800-922-0204 FL",
       "Utilities & Bills", "RENT_AND_UTILITIES_TELEPHONE", "Communications-Mobile Telecom",
       "84.00", "92.00", channel="online", mcc="4814", website="verizon.com"),
    _m("comcast", "Comcast", "COMCAST CALIFORNIA", "COMCAST CALIFORNIA 800-266-2278 CA",
       "Utilities & Bills", "RENT_AND_UTILITIES_INTERNET_AND_CABLE",
       "Communications-Cable & Internet Comm", "70.00", "75.00", channel="online", mcc="4899",
       website="xfinity.com"),
    _m("pge", "PG&E", "PG&E WEB ONLINE", "PG&E WEB ONLINE 800-743-5000 CA", "Utilities & Bills",
       "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", "Business Services-Utilities", "55.00",
       "95.00", channel="online", mcc="4900", website="pge.com"),
    _m("lemonade", "Lemonade", "LEMONADE INSURANCE", "LEMONADE I* LEMONADE.COM NY",
       "Utilities & Bills", "GENERAL_SERVICES_INSURANCE", "Business Services-Insurance Services",
       "174.00", "186.00", channel="online", mcc="6300", website="lemonade.com"),
    # ------------------------------------------------------------------ services
    _m("usps", "USPS", "USPS PO 0574410095", "USPS PO 0574410095 SAN FRANCISCO CA", "Services",
       "GENERAL_SERVICES_POSTAGE_AND_SHIPPING", "Business Services-Mailing & Shipping", "4.80",
       "38.00", mcc="9402", website="usps.com",
       place=_sf("4083 24th St", "94114", "37.7513", "-122.4336", "0574410095")),
    _m("ups_store", "The UPS Store", "THE UPS STORE 4021", "THE UPS STORE 4021 SAN FRANCISCO CA",
       "Services", "GENERAL_SERVICES_POSTAGE_AND_SHIPPING", "Business Services-Mailing & Shipping",
       "9.50", "64.00", mcc="4215", website="theupsstore.com",
       place=_sf("2261 Market St", "94114", "37.7650", "-122.4318", "4021")),
    _m("dmv", "California DMV", "CA DMV REGISTRATION", "CA DMV REGISTRATION SACRAMENTO CA",
       "Services", "GOVERNMENT_AND_NON_PROFIT_GOVERNMENT_DEPARTMENTS_AND_AGENCIES",
       "Other-Government Services", "198.00", "212.00", channel="online", mcc="9399",
       website="dmv.ca.gov"),
    # ------------------------------------------------------------------ gifts & donations
    _m("kqed", "KQED", "KQED INC", "KQED INC SAN FRANCISCO CA", "Gifts & Donations",
       "GOVERNMENT_AND_NON_PROFIT_DONATIONS", "Other-Charities", "10.00", "10.00",
       channel="online", mcc="8398", website="kqed.org"),
    _m("wikimedia", "Wikimedia Foundation", "WIKIMEDIA FOUNDATION", "WIKIMEDIA 877-600-9454 CA",
       "Gifts & Donations", "GOVERNMENT_AND_NON_PROFIT_DONATIONS", "Other-Charities", "25.00",
       "25.00", channel="online", mcc="8398", website="wikimediafoundation.org"),
    _m("paper_source", "Paper Source", "PAPER SOURCE #12", "PAPER SOURCE #12 SAN FRANCISCO CA",
       "Gifts & Donations", "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES",
       "Merchandise & Supplies-General Retail", "9.00", "64.00", mcc="5947",
       website="papersource.com",
       place=_sf("2061 Chestnut St", "94123", "37.8005", "-122.4380", "12")),
    _m("uncommon_goods", "UncommonGoods", "UNCOMMONGOODS", "UNCOMMONGOODS 888-365-0056 NY",
       "Gifts & Donations", "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES",
       "Merchandise & Supplies-Mail Order", "28.00", "96.00", channel="online", mcc="5947",
       website="uncommongoods.com"),
    # ------------------------------------------------------------------ education
    _m("coursera", "Coursera", "COURSERA.ORG", "COURSERA.ORG MOUNTAIN VIEW CA", "Education",
       "GENERAL_SERVICES_EDUCATION", "Business Services-Other Services", "49.00", "49.00",
       channel="online", mcc="8299", website="coursera.org"),
    _m("masterclass", "MasterClass", "MASTERCLASS.COM", "MASTERCLASS SAN FRANCISCO CA",
       "Education", "GENERAL_SERVICES_EDUCATION", "Business Services-Other Services", "120.00",
       "120.00", channel="online", mcc="8299", website="masterclass.com"),
    _m("udemy", "Udemy", "UDEMY", "UDEMY ONLINE COURSES SAN FRANCISCO CA", "Education",
       "GENERAL_SERVICES_EDUCATION", "Business Services-Other Services", "12.99", "19.99",
       channel="online", mcc="8299", website="udemy.com"),
    # ------------------------------------------------------------------ other
    _m("venmo", "Venmo", "VENMO *{v}", "VENMO *{v} VISA DIRECT NY", "Other",
       "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "Other-Miscellaneous", "24.00", "180.00",
       channel="online", mcc="4829", website="venmo.com", confidence="MEDIUM",
       variants=("JORDAN LEE", "PRIYA SHAH", "MARCO DIAZ")),
    # ------------------------------------------------------------------ pets (a user rule)
    _m("chewy", "Chewy", "CHEWY.COM", "CHEWY.COM 800-672-4399 FL", "Pets",
       "GENERAL_MERCHANDISE_PET_SUPPLIES", INTERNET, "52.48", "54.98", channel="online",
       mcc="5995", website="chewy.com"),
    _m("petco", "Petco", "PETCO 1235", "PETCO 1235 SAN FRANCISCO CA", "Pets",
       "GENERAL_MERCHANDISE_PET_SUPPLIES", "Merchandise & Supplies-Pet Stores", "9.99", "48.00",
       mcc="5995", website="petco.com",
       place=_sf("1685 Bryant St", "94103", "37.7672", "-122.4105", "1235")),
    _m("banfield", "Banfield Pet Hospital", "BANFIELD PET HOSP #0612",
       "BANFIELD PET HOSP #0612 SAN FRANCISCO CA", "Pets", "MEDICAL_VETERINARY_SERVICES",
       "Business Services-Health Care Services", "86.00", "262.00", mcc="0742",
       website="banfield.com",
       place=_sf("1591 Bryant St", "94103", "37.7681", "-122.4103", "0612")),
    # ------------------------------------------------------------------ no PFC: Uncategorized
    _m("popup", None, "{v}", "{v} SAN FRANCISCO CA", "Uncategorized", None,
       "Other-Miscellaneous", "8.00", "42.00", confidence="LOW",
       variants=("SQ *GOLDEN GATE POP UP", "TST* NIGHT MARKET STALL 12", "ZETTLE_*ARTISAN FAIR",
                 "SQ *DOLORES PARK CART")),
    # ------------------------------------------------------------------ travel booked from home
    _m("united", "United Airlines", "UNITED 0162{code}", "UNITED 0162{code} 800-932-2732 TX",
       "Travel", "TRAVEL_FLIGHTS", "Travel-Airline", "248.00", "720.00", channel="online",
       mcc="3000", website="united.com"),
    _m("alaska", "Alaska Airlines", "ALASKA AIR 0272{code}", "ALASKA AIR 0272{code} SEATTLE WA",
       "Travel", "TRAVEL_FLIGHTS", "Travel-Airline", "186.00", "420.00", channel="online",
       mcc="3058", website="alaskaair.com"),
    _m("delta", "Delta Air Lines", "DELTA AIR LINES", "DELTA AIR 0062{code} ATLANTA GA", "Travel",
       "TRAVEL_FLIGHTS", "Travel-Airline", "312.00", "640.00", channel="online", mcc="3058",
       website="delta.com"),
    _m("airbnb", "Airbnb", "AIRBNB * HM{code}", "AIRBNB * HM{code} AIRBNB.COM CA", "Travel",
       "TRAVEL_LODGING", "Travel-Lodging", "380.00", "1180.00", channel="online", mcc="7011",
       website="airbnb.com"),
]

# Merchants seen only on trips (the Lisbon ones are charged in USD plus a 2.7% foreign fee).
_TRIP_MERCHANTS = [
    # Seattle (CSV era)
    _m("andra", "Hotel Ändra", "HOTEL ANDRA", "HOTEL ANDRA SEATTLE WA", "Travel", "TRAVEL_LODGING",
       "Travel-Lodging", "540.00", "690.00", mcc="7011",
       place=_at("Seattle", "WA", "2000 4th Ave", "98121", "47.6131", "-122.3398")),
    _m("pike_chowder", "Pike Place Chowder", "PIKE PLACE CHOWDER",
       "PIKE PLACE CHOWDER SEATTLE WA", "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT",
       RESTAURANT, "16.00", "38.00", mcc="5812",
       place=_at("Seattle", "WA", "1530 Post Aly", "98101", "47.6091", "-122.3412")),
    _m("roastery", "Starbucks", "STARBUCKS RESERVE ROASTERY",
       "STARBUCKS RESERVE ROASTERY SEATTLE WA", "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE,
       "8.50", "24.00", mcc="5814",
       place=_at("Seattle", "WA", "1124 Pike St", "98101", "47.6141", "-122.3281")),
    _m("storyville", "Storyville Coffee", "STORYVILLE COFFEE", "STORYVILLE COFFEE SEATTLE WA",
       "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE, "5.50", "14.00", mcc="5814",
       place=_at("Seattle", "WA", "94 Pike St", "98101", "47.6089", "-122.3404")),
    _m("elm_coffee", "Elm Coffee Roasters", "ELM COFFEE ROASTERS",
       "ELM COFFEE ROASTERS SEATTLE WA", "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE, "4.75",
       "11.50", mcc="5814",
       place=_at("Seattle", "WA", "240 2nd Ave S", "98104", "47.5997", "-122.3318")),
    _m("piroshky", "Piroshky Piroshky", "PIROSHKY PIROSHKY", "PIROSHKY PIROSHKY SEATTLE WA",
       "Dining & Drinks", "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", RESTAURANT, "7.50", "21.00",
       mcc="5462", place=_at("Seattle", "WA", "1908 Pike Pl", "98101", "47.6100", "-122.3426")),
    _m("pink_door", "The Pink Door", "THE PINK DOOR", "THE PINK DOOR SEATTLE WA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "64.00", "138.00", mcc="5812",
       place=_at("Seattle", "WA", "1919 Post Aly", "98101", "47.6103", "-122.3426")),
    _m("dtf_seattle", "Din Tai Fung", "DIN TAI FUNG PACIFIC PL", "DIN TAI FUNG 600 PINE SEATTLE WA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "38.00", "86.00", mcc="5812",
       place=_at("Seattle", "WA", "600 Pine St", "98101", "47.6125", "-122.3355")),
    _m("seattle_art", "Seattle Art Museum", "SEATTLE ART MUSEUM",
       "SEATTLE ART MUSEUM SEATTLE WA", "Entertainment",
       "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
       "Entertainment-Other Entertainment", "32.99", "32.99", mcc="7991",
       place=_at("Seattle", "WA", "1300 1st Ave", "98101", "47.6073", "-122.3381")),
    _m("elliott_bay", "Elliott Bay Book Company", "ELLIOTT BAY BOOK CO",
       "ELLIOTT BAY BOOK CO SEATTLE WA", "Shopping",
       "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS", "Merchandise & Supplies-Book Stores",
       "14.00", "48.00", mcc="5942",
       place=_at("Seattle", "WA", "1521 10th Ave", "98122", "47.6147", "-122.3195")),
    # Los Angeles (the two suspect CSV duplicates)
    _m("ace_hotel", "Ace Hotel", "ACE HOTEL DTLA", "ACE HOTEL DOWNTOWN LA LOS ANGELES CA",
       "Travel", "TRAVEL_LODGING", "Travel-Lodging", "880.00", "980.00", mcc="7011",
       website="acehotel.com",
       place=_at("Los Angeles", "CA", "929 S Broadway", "90015", "34.0412", "-118.2567")),
    _m("hertz", "Hertz", "HERTZ RENT-A-CAR", "HERTZ RENT-A-CAR LOS ANGELES CA", "Travel",
       "TRAVEL_RENTAL_CARS", "Travel-Vehicle Rental", "196.00", "248.00", mcc="3357",
       website="hertz.com",
       place=_at("Los Angeles", "CA", "9000 Airport Blvd", "90045", "33.9550", "-118.3830")),
    _m("grand_central", "Grand Central Market", "GRAND CENTRAL MARKET",
       "GRAND CENTRAL MARKET LOS ANGELES CA", "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT",
       RESTAURANT, "14.00", "42.00", mcc="5812",
       place=_at("Los Angeles", "CA", "317 S Broadway", "90013", "34.0508", "-118.2489")),
    _m("starbucks_la", "Starbucks", "STARBUCKS STORE 10421",
       "STARBUCKS STORE 10421 LOS ANGELES CA", "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE,
       "4.45", "9.85", mcc="5814", website="starbucks.com",
       place=_at("Los Angeles", "CA", "700 S Flower St", "90017", "34.0487", "-118.2581")),
    _m("intelligentsia_la", "Intelligentsia Coffee", "INTELLIGENTSIA SILVER LAKE",
       "INTELLIGENTSIA COFFEE SILVER LAKE LOS ANGELES CA", "Dining & Drinks",
       "FOOD_AND_DRINK_COFFEE", CAFE, "5.25", "12.50", mcc="5814",
       place=_at("Los Angeles", "CA", "3922 W Sunset Blvd", "90029", "34.0912", "-118.2795")),
    _m("verve_la", "Verve Coffee Roasters", "VERVE COFFEE SPRING ST",
       "VERVE COFFEE ROASTERS LOS ANGELES CA", "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE,
       "5.00", "11.75", mcc="5814",
       place=_at("Los Angeles", "CA", "833 S Spring St", "90014", "34.0428", "-118.2547")),
    _m("philippe", "Philippe the Original", "PHILIPPE THE ORIGINAL",
       "PHILIPPE THE ORIGINAL LOS ANGELES CA", "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT",
       RESTAURANT, "13.00", "28.00", mcc="5812",
       place=_at("Los Angeles", "CA", "1001 N Alameda St", "90012", "34.0597", "-118.2366")),
    _m("sqirl", "Sqirl", "SQIRL", "SQ *SQIRL LOS ANGELES CA", "Dining & Drinks",
       "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "18.00", "42.00", mcc="5812",
       place=_at("Los Angeles", "CA", "720 N Virgil Ave", "90029", "34.0838", "-118.2871")),
    _m("guisados", "Guisados", "GUISADOS DTLA", "GUISADOS DTLA LOS ANGELES CA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "14.00", "32.00", mcc="5812",
       place=_at("Los Angeles", "CA", "541 S Spring St", "90013", "34.0477", "-118.2503")),
    _m("howlin_rays", "Howlin' Ray's", "HOWLIN RAYS", "HOWLIN RAYS LOS ANGELES CA",
       "Dining & Drinks", "FOOD_AND_DRINK_FAST_FOOD", RESTAURANT, "16.00", "34.00", mcc="5814",
       place=_at("Los Angeles", "CA", "727 N Broadway", "90012", "34.0614", "-118.2385")),
    _m("bestia", "Bestia", "BESTIA", "BESTIA LOS ANGELES CA", "Dining & Drinks",
       "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "92.00", "186.00", mcc="5812",
       place=_at("Los Angeles", "CA", "2121 E 7th Pl", "90021", "34.0339", "-118.2294")),
    # Lake Tahoe
    _m("heavenly", "Heavenly Mountain Resort", "HEAVENLY MOUNTAIN RESORT",
       "HEAVENLY MTN RESORT SOUTH LAKE TAHOE CA", "Entertainment",
       "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
       "Entertainment-Sports & Recreation", "278.00", "312.00", mcc="7032",
       place=_at("South Lake Tahoe", "CA", "4080 Lake Tahoe Blvd", "96150", "38.9566",
                 "-119.9405")),
    _m("shell_truckee", "Shell", "SHELL OIL 57444", "SHELL OIL 57444 TRUCKEE CA",
       "Transportation", "TRANSPORTATION_GAS", "Transportation-Fuel", "58.00", "74.00",
       mcc="5541",
       place=_at("Truckee", "CA", "11280 Donner Pass Rd", "96161", "39.3237", "-120.2108")),
    _m("raleys", "Raley's", "RALEY'S #304", "RALEY'S #304 SOUTH LAKE TAHOE CA", "Groceries",
       "FOOD_AND_DRINK_GROCERIES", GROCERIES, "68.00", "112.00", mcc="5411",
       place=_at("South Lake Tahoe", "CA", "4000 Lake Tahoe Blvd", "96150", "38.9551",
                 "-119.9440")),
    _m("loft_tahoe", "The Loft", "THE LOFT TAHOE", "THE LOFT SOUTH LAKE TAHOE CA",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "96.00", "148.00",
       mcc="5812",
       place=_at("South Lake Tahoe", "CA", "1171 Emerald Bay Rd", "96150", "38.9333",
                 "-119.9842")),
    # New York (a conference)
    _m("marriott", "Marriott", "MARRIOTT MARQUIS NYC", "MARRIOTT MARQUIS NEW YORK NY", "Travel",
       "TRAVEL_LODGING", "Travel-Lodging", "612.40", "612.40", mcc="3509",
       website="marriott.com",
       place=_at("New York", "NY", "1535 Broadway", "10036", "40.7585", "-73.9858")),
    _m("katz", "Katz's Delicatessen", "KATZ'S DELICATESSEN", "KATZ'S DELICATESSEN NEW YORK NY",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "32.00", "54.00", mcc="5812",
       place=_at("New York", "NY", "205 E Houston St", "10002", "40.7223", "-73.9874")),
    _m("joes_pizza", "Joe's Pizza", "JOE'S PIZZA", "JOE'S PIZZA NEW YORK NY", "Dining & Drinks",
       "FOOD_AND_DRINK_FAST_FOOD", RESTAURANT, "9.50", "18.00", mcc="5814",
       place=_at("New York", "NY", "7 Carmine St", "10014", "40.7306", "-74.0021")),
    _m("carbone", "Carbone", "CARBONE", "CARBONE NEW YORK NY", "Dining & Drinks",
       "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "286.40", "286.40", mcc="5812",
       place=_at("New York", "NY", "181 Thompson St", "10012", "40.7279", "-74.0000")),
    _m("mta", "MTA", "MTA*NYCT PAYGO", "MTA*NYCT PAYGO NEW YORK NY", "Transportation",
       "TRANSPORTATION_PUBLIC_TRANSIT", "Transportation-Rail Services", "2.90", "2.90",
       channel="other", mcc="4111", website="mta.info"),
    _m("the_met", "The Met", "THE MET FIFTH AVENUE", "THE METROPOLITAN MUSEUM NEW YORK NY",
       "Entertainment", "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
       "Entertainment-Other Entertainment", "30.00", "30.00", mcc="7991",
       place=_at("New York", "NY", "1000 5th Ave", "10028", "40.7794", "-73.9632")),
    _m("shake_shack", "Shake Shack", "SHAKE SHACK MADISON SQ",
       "SHAKE SHACK MADISON SQ NEW YORK NY", "Dining & Drinks", "FOOD_AND_DRINK_FAST_FOOD",
       RESTAURANT, "14.00", "26.00", mcc="5814", website="shakeshack.com",
       place=_at("New York", "NY", "Madison Ave & E 23rd St", "10010", "40.7414", "-73.9882")),
    _m("los_tacos", "Los Tacos No. 1", "LOS TACOS NO 1", "LOS TACOS NO 1 NEW YORK NY",
       "Dining & Drinks", "FOOD_AND_DRINK_FAST_FOOD", RESTAURANT, "12.00", "24.00", mcc="5814",
       place=_at("New York", "NY", "75 9th Ave", "10011", "40.7424", "-74.0061")),
    _m("russ_daughters", "Russ & Daughters", "RUSS & DAUGHTERS", "RUSS & DAUGHTERS NEW YORK NY",
       "Dining & Drinks", "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", RESTAURANT, "14.00", "38.00",
       mcc="5812", place=_at("New York", "NY", "179 E Houston St", "10002", "40.7223", "-73.9882")),
    _m("levain", "Levain Bakery", "LEVAIN BAKERY", "LEVAIN BAKERY NEW YORK NY", "Dining & Drinks",
       "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", CAFE, "5.00", "16.00", mcc="5462",
       place=_at("New York", "NY", "167 W 74th St", "10023", "40.7799", "-73.9803")),
    _m("joe_coffee", "Joe Coffee", "JOE COFFEE WAVERLY", "JOE COFFEE WAVERLY PL NEW YORK NY",
       "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE, "4.50", "10.00", mcc="5814",
       place=_at("New York", "NY", "141 Waverly Pl", "10014", "40.7337", "-74.0002")),
    _m("via_carota", "Via Carota", "VIA CAROTA", "VIA CAROTA NEW YORK NY", "Dining & Drinks",
       "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "78.00", "164.00", mcc="5812",
       place=_at("New York", "NY", "51 Grove St", "10014", "40.7332", "-74.0039")),
    # Lisbon (abroad: charged in USD, each with a separate foreign transaction fee)
    _m("tap", "TAP Air Portugal", "TAP AIR PORTUGAL", "TAP AIR PORTUGAL LISBOA PRT", "Travel",
       "TRAVEL_FLIGHTS", "Travel-Airline", "1148.60", "1148.60", channel="online", mcc="3091",
       website="flytap.com", foreign=True),
    _m("time_out", "Time Out Market Lisboa", "TIME OUT MARKET LISBOA",
       "TIME OUT MARKET LISBOA LISBOA PRT", "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT",
       RESTAURANT, "18.00", "46.00", mcc="5812", foreign=True,
       place=_at("Lisboa", None, "Av. 24 de Julho 49", "1200-479", "38.7069", "-9.1457")),
    _m("pasteis", "Pastéis de Belém", "PASTEIS DE BELEM", "PASTEIS DE BELEM LISBOA PRT",
       "Dining & Drinks", "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", CAFE, "6.00", "16.00",
       mcc="5462", foreign=True,
       place=_at("Lisboa", None, "R. de Belém 84", "1300-085", "38.6975", "-9.2032")),
    _m("cevicheria", "A Cevicheria", "A CEVICHERIA", "A CEVICHERIA LISBOA PRT",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "62.00", "118.00",
       mcc="5812", foreign=True,
       place=_at("Lisboa", None, "R. Dom Pedro V 129", "1250-096", "38.7158", "-9.1470")),
    _m("pingo_doce", "Pingo Doce", "PINGO DOCE LISBOA", "PINGO DOCE LISBOA PRT", "Groceries",
       "FOOD_AND_DRINK_GROCERIES", GROCERIES, "12.00", "38.00", mcc="5411", foreign=True,
       place=_at("Lisboa", None, "R. do Arsenal 7", "1100-038", "38.7079", "-9.1367")),
    _m("cp", "CP Comboios de Portugal", "CP COMBOIOS DE PORTUGAL",
       "CP COMBOIOS PORTUGAL LISBOA PRT", "Transportation", "TRANSPORTATION_PUBLIC_TRANSIT",
       "Transportation-Rail Services", "5.20", "24.00", mcc="4112", foreign=True,
       place=_at("Lisboa", None, "Estação do Rossio", "1200-091", "38.7144", "-9.1411")),
    _m("uber_lisbon", "Uber", "UBER *TRIP", "UBER *TRIP LISBOA PRT", "Transportation",
       "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "Transportation-Taxis & Coach", "6.40", "22.00",
       channel="online", mcc="4121", website="uber.com", foreign=True),
    _m("gulbenkian", "Calouste Gulbenkian Museum", "MUSEU GULBENKIAN",
       "MUSEU GULBENKIAN LISBOA PRT", "Entertainment",
       "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
       "Entertainment-Other Entertainment", "16.00", "24.00", mcc="7991", foreign=True,
       place=_at("Lisboa", None, "Av. de Berna 45A", "1067-001", "38.7372", "-9.1540")),
    _m("manteigaria", "Manteigaria", "MANTEIGARIA CHIADO", "MANTEIGARIA LISBOA PRT",
       "Dining & Drinks", "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", CAFE, "3.20", "12.00",
       mcc="5462", foreign=True,
       place=_at("Lisboa", None, "R. do Loreto 2", "1200-242", "38.7107", "-9.1445")),
    _m("brasileira", "A Brasileira", "A BRASILEIRA", "CAFE A BRASILEIRA LISBOA PRT",
       "Dining & Drinks", "FOOD_AND_DRINK_COFFEE", CAFE, "3.50", "14.00", mcc="5814",
       foreign=True, place=_at("Lisboa", None, "R. Garrett 120", "1200-273", "38.7107", "-9.1420")),
    _m("ramiro", "Cervejaria Ramiro", "CERVEJARIA RAMIRO", "CERVEJARIA RAMIRO LISBOA PRT",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "54.00", "128.00", mcc="5812",
       foreign=True,
       place=_at("Lisboa", None, "Av. Almirante Reis 1", "1150-007", "38.7206", "-9.1356")),
    _m("taberna_flores", "Taberna da Rua das Flores", "TABERNA RUA DAS FLORES",
       "TABERNA DA RUA DAS FLORES LISBOA PRT", "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT",
       RESTAURANT, "36.00", "84.00", mcc="5812", foreign=True,
       place=_at("Lisboa", None, "R. das Flores 103", "1200-194", "38.7101", "-9.1446")),
    _m("ze_mouraria", "Zé da Mouraria", "ZE DA MOURARIA", "ZE DA MOURARIA LISBOA PRT",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "22.00", "48.00", mcc="5812",
       foreign=True,
       place=_at("Lisboa", None, "R. João do Outeiro 24", "1100-292", "38.7155", "-9.1352")),
    _m("sao_bento", "Café de São Bento", "CAFE DE SAO BENTO", "CAFE DE SAO BENTO LISBOA PRT",
       "Dining & Drinks", "FOOD_AND_DRINK_RESTAURANT", RESTAURANT, "58.00", "112.00",
       mcc="5812", foreign=True,
       place=_at("Lisboa", None, "R. de São Bento 212", "1200-821", "38.7139", "-9.1531")),
    _m("oceanario", "Oceanário de Lisboa", "OCEANARIO DE LISBOA",
       "OCEANARIO DE LISBOA LISBOA PRT", "Entertainment",
       "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
       "Entertainment-Other Entertainment", "50.00", "56.00", mcc="7991", foreign=True,
       place=_at("Lisboa", None, "Esplanada Dom Carlos I", "1990-005", "38.7635", "-9.0937")),
    _m("metro_lisboa", "Metropolitano de Lisboa", "METROPOLITANO DE LISBOA",
       "METROPOLITANO DE LISBOA LISBOA PRT", "Transportation", "TRANSPORTATION_PUBLIC_TRANSIT",
       "Transportation-Rail Services", "1.95", "8.00", channel="other", mcc="4111",
       foreign=True),
]
# fmt: on

#: Where the persona lives: an in-store charge here can't happen while they are away on a trip.
HOME_CITIES = frozenset({"San Francisco", "Daly City", "Emeryville"})


def is_home_store(merchant: Merchant) -> bool:
    """An in-person purchase in the persona's home area (impossible on a trip day)."""
    if merchant.channel != "in store":
        return False
    if merchant.place is not None:
        return merchant.place.city in HOME_CITIES
    return any(city.upper() in merchant.raw for city in HOME_CITIES)


MERCHANTS: dict[str, Merchant] = {m.key: m for m in _MERCHANTS + _TRIP_MERCHANTS}

# Monthly budgets the persona keeps (current amounts), close to its usual spending.
MONTHLY_BUDGETS: dict[str, Decimal] = {
    "Groceries": Decimal("700.00"),
    "Dining & Drinks": Decimal("650.00"),
    "Shopping": Decimal("450.00"),
    "Transportation": Decimal("325.00"),
    "Entertainment": Decimal("150.00"),
    "Subscriptions & Streaming": Decimal("120.00"),
    "Health & Fitness": Decimal("300.00"),
    "Personal Care": Decimal("120.00"),
    "Home": Decimal("150.00"),
    "Utilities & Bills": Decimal("325.00"),
    "Pets": Decimal("120.00"),
}
ANNUAL_BUDGETS: dict[str, Decimal] = {
    "Travel": Decimal("6000.00"),
    "Gifts & Donations": Decimal("800.00"),
}
OVERALL_TARGET = Decimal("3800.00")
#: This month: the gym (billed on the 1st, $255) is 85 % of Health & Fitness's $300 budget.
NEARLY_FULL = "Health & Fitness"
#: This month: concert tickets put Entertainment over budget.
OVERSPENT = "Entertainment"
ROLLOVER = "Personal Care"


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """A user rule (``services.categorize`` matching semantics)."""

    name: str
    category: str
    priority: int
    merchant_pattern: str | None = None
    description_pattern: str | None = None
    match_type: str = "contains"
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    is_active: bool = True
    #: A one-click rule ("Always categorize like this") made from a row of this merchant.
    from_merchant: str | None = None


# fmt: off
RULES: tuple[RuleSpec, ...] = (
    RuleSpec("Small Apple charges are subscriptions", "Subscriptions & Streaming", 10,
             merchant_pattern="apple", match_type="starts_with", amount_min=Decimal("0.01"),
             amount_max=Decimal("20.00")),
    RuleSpec("Streaming and news", "Subscriptions & Streaming", 20,
             merchant_pattern="netflix|spotify|openai|new york times|nytimes|amazon prime",
             match_type="regex"),
    RuleSpec("Costco is groceries", "Groceries", 50, merchant_pattern="costco",
             from_merchant="costco"),
    RuleSpec("Pet stores and the vet", "Pets", 50, merchant_pattern="chewy|petco|banfield",
             match_type="regex"),
    RuleSpec("Gym", "Health & Fitness", 60, merchant_pattern="equinox"),
    RuleSpec("PG&E", "Utilities & Bills", 60, merchant_pattern="pg&e"),
    RuleSpec("Uber rides on trips", "Travel", 100, merchant_pattern="uber", is_active=False),
)
# fmt: on
