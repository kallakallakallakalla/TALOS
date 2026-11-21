"""Where the airports are: the six German departure airports and every destination a carrier
of the start kit serves. Used for the route map and for great-circle distances.

Coordinates are the airports' published reference points, rounded to 0.01 degrees.
"""

import math

# IATA code -> (city, latitude, longitude)
AIRPORTS: dict[str, tuple[str, float, float]] = {
    # departure airports (pickup_zones.csv)
    "CGN": ("Cologne Bonn", 50.87, 7.14),
    "DUS": ("Düsseldorf", 51.29, 6.77),
    "FRA": ("Frankfurt", 50.03, 8.57),
    "HAM": ("Hamburg", 53.63, 9.99),
    "MUC": ("Munich", 48.35, 11.79),
    "STR": ("Stuttgart", 48.69, 9.22),
    # destinations (carrier-list.csv, column `serves`)
    "ASU": ("Asunción", -25.24, -57.52),
    "ATL": ("Atlanta", 33.64, -84.43),
    "BKK": ("Bangkok", 13.69, 100.75),
    "BOG": ("Bogotá", 4.70, -74.15),
    "BOM": ("Mumbai", 19.09, 72.87),
    "CAI": ("Cairo", 30.12, 31.41),
    "DEL": ("Delhi", 28.56, 77.10),
    "DFW": ("Dallas/Fort Worth", 32.90, -97.04),
    "DOH": ("Doha", 25.27, 51.61),
    "DXB": ("Dubai", 25.25, 55.36),
    "EZE": ("Buenos Aires", -34.82, -58.54),
    "GRU": ("São Paulo", -23.43, -46.47),
    "HKG": ("Hong Kong", 22.31, 113.91),
    "ICN": ("Seoul", 37.46, 126.44),
    "JFK": ("New York", 40.64, -73.78),
    "JNB": ("Johannesburg", -26.14, 28.25),
    "KTM": ("Kathmandu", 27.70, 85.36),
    "KUL": ("Kuala Lumpur", 2.75, 101.71),
    "LAX": ("Los Angeles", 33.94, -118.41),
    "LIM": ("Lima", -12.02, -77.11),
    "LOS": ("Lagos", 6.58, 3.32),
    "MEL": ("Melbourne", -37.67, 144.84),
    "MEX": ("Mexico City", 19.44, -99.07),
    "MIA": ("Miami", 25.79, -80.29),
    "MNL": ("Manila", 14.51, 121.02),
    "NBO": ("Nairobi", -1.32, 36.93),
    "NRT": ("Tokyo", 35.77, 140.39),
    "ORD": ("Chicago", 41.98, -87.90),
    "PEK": ("Beijing", 40.08, 116.58),
    "PPT": ("Papeete", -17.55, -149.61),
    "PVG": ("Shanghai", 31.14, 121.81),
    "RUH": ("Riyadh", 24.96, 46.70),
    "SCL": ("Santiago", -33.39, -70.79),
    "SGN": ("Ho Chi Minh City", 10.82, 106.65),
    "SIN": ("Singapore", 1.36, 103.99),
    "SYD": ("Sydney", -33.95, 151.18),
    "TLV": ("Tel Aviv", 32.01, 34.89),
    "TNR": ("Antananarivo", -18.80, 47.48),
    "TPE": ("Taipei", 25.08, 121.23),
    "ULN": ("Ulaanbaatar", 47.65, 106.82),
    "YYZ": ("Toronto", 43.68, -79.63),
}

_EARTH_RADIUS_KM = 6371.0


def distance_km(origin: str | None, destination: str | None) -> int | None:
    """Great-circle distance between two airports, or None if one of them is unknown."""
    if origin not in AIRPORTS or destination not in AIRPORTS:
        return None
    (_, lat1, lon1), (_, lat2, lon2) = AIRPORTS[origin], AIRPORTS[destination]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return round(2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a)))
