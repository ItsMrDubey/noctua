"""Hand-defined strategic chokepoints as approximate lon/lat polygons."""

CHOKEPOINTS: list[dict] = [
    {"name": "Strait of Hormuz", "note": "Persian Gulf outlet; ~20% of world oil trade.",
     "ring": [(55.9, 26.9), (56.6, 27.1), (57.1, 26.2), (56.6, 25.6), (56.1, 25.7), (56.0, 26.3)]},
    {"name": "Bab-el-Mandeb", "note": "Red Sea / Gulf of Aden gateway between Yemen and Djibouti/Eritrea.",
     "ring": [(42.9, 12.9), (43.7, 12.9), (43.6, 12.3), (43.2, 12.2), (42.8, 12.5)]},
    {"name": "Suez Canal", "note": "Mediterranean to Red Sea link; ~12% of world trade.",
     "ring": [(32.2, 31.35), (32.45, 31.3), (32.65, 29.95), (32.45, 29.85), (32.35, 30.4)]},
    {"name": "Strait of Malacca", "note": "Shortest Indian Ocean to Pacific route; ~25% of traded goods.",
     "ring": [(98.5, 4.2), (99.8, 4.0), (101.4, 2.9), (103.5, 1.3), (103.2, 1.1), (101.0, 2.3), (99.2, 3.2), (98.2, 3.8)]},
    {"name": "Singapore Strait", "note": "Narrow channel at the southern end of Malacca.",
     "ring": [(103.5, 1.35), (104.3, 1.4), (104.3, 1.15), (103.6, 1.1)]},
    {"name": "Bosphorus", "note": "Turkish strait linking the Black Sea and Marmara; Montreux Convention.",
     "ring": [(28.95, 41.25), (29.2, 41.25), (29.15, 40.98), (28.95, 41.0)]},
    {"name": "Dardanelles", "note": "Turkish strait linking Marmara and the Aegean.",
     "ring": [(26.1, 40.2), (26.8, 40.5), (26.8, 40.3), (26.4, 40.0), (26.15, 40.0)]},
    {"name": "Strait of Gibraltar", "note": "Atlantic to Mediterranean entrance.",
     "ring": [(-5.9, 36.2), (-5.2, 36.2), (-5.2, 35.8), (-5.9, 35.8)]},
    {"name": "Panama Canal", "note": "Atlantic to Pacific canal; draught-limited by Gatun Lake levels.",
     "ring": [(-79.95, 9.4), (-79.5, 9.4), (-79.5, 8.85), (-79.95, 8.85)]},
    {"name": "Taiwan Strait", "note": "Between PRC and Taiwan; major PLA and transit activity.",
     "ring": [(118.5, 25.8), (120.8, 25.5), (120.2, 22.9), (118.3, 23.2)]},
    {"name": "Lombok Strait", "note": "Deep-water alternative to Malacca between Bali and Lombok.",
     "ring": [(115.5, -8.1), (116.0, -8.1), (116.0, -8.9), (115.5, -8.9)]},
    {"name": "Danish Straits", "note": "Oresund, Great Belt and Little Belt: Baltic Sea access.",
     "ring": [(10.4, 57.8), (12.8, 56.3), (12.9, 55.4), (11.0, 54.8), (9.8, 55.2), (10.0, 56.7)]},
    {"name": "English Channel (Dover)", "note": "World's busiest shipping lane; Dover Strait TSS.",
     "ring": [(1.0, 51.2), (2.0, 51.3), (1.7, 50.7), (0.9, 50.7)]},
    {"name": "Cape of Good Hope route", "note": "Alternative to Suez/Bab-el-Mandeb around southern Africa.",
     "ring": [(17.0, -34.0), (21.0, -34.6), (21.0, -35.8), (17.0, -35.5)]},
    {"name": "Kerch Strait", "note": "Black Sea to Sea of Azov; Crimean Bridge.",
     "ring": [(36.4, 45.5), (36.8, 45.4), (36.7, 45.0), (36.4, 45.0)]},
    {"name": "GIUK gap", "note": "Greenland-Iceland-UK gap: NATO anti-submarine choke on Russian Northern Fleet.",
     "ring": [(-27.0, 66.0), (-13.0, 65.0), (-8.0, 60.5), (-4.0, 58.5), (-6.0, 56.5), (-18.0, 58.5), (-30.0, 62.0)]},
]


def chokepoint_features() -> list[dict]:
    out = []
    for c in CHOKEPOINTS:
        ring = [list(p) for p in c["ring"]]
        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        out.append({
            "name": c["name"], "note": c["note"], "ring": ring,
            "lon": sum(lons[:-1]) / (len(lons) - 1), "lat": sum(lats[:-1]) / (len(lats) - 1),
        })
    return out
