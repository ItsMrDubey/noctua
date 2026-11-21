"""
Known military bases, airfields, and key infrastructure in the Middle East / CENTCOM AOR.
Used for OSINT overlay generation (KML), proximity correlation, and the web dashboard.
"""

import json
import math
import os
import xml.sax.saxutils as saxutils

# ---------------------------------------------------------------------------
# Master facility list
# ---------------------------------------------------------------------------
# Each entry: name, lat, lon, type, country, operator, notes
#
# type values : airbase, naval_base, port, radar, missile_site, refinery,
#               nuclear, garrison, logistics, air_defense, drone_base
# operator    : US, UK, France, Russia, Iran, Israel, Saudi, UAE, Turkey,
#               Qatar, Kuwait, Bahrain, Oman, Jordan, Egypt, Iraq, Djibouti
# ---------------------------------------------------------------------------

BASES = [
    # -----------------------------------------------------------------------
    # UNITED STATES
    # -----------------------------------------------------------------------
    {
        "name": "Al Udeid Air Base",
        "lat": 25.1173,
        "lon": 51.3150,
        "type": "airbase",
        "country": "Qatar",
        "operator": "US",
        "notes": "CENTCOM Combined Air Operations Center (CAOC). Main US air hub in the Gulf.",
    },
    {
        "name": "Al Dhafra Air Base",
        "lat": 24.2482,
        "lon": 54.5477,
        "type": "airbase",
        "country": "UAE",
        "operator": "US",
        "notes": "Hosts USAF F-22s, KC-135s, RQ-4 Global Hawks, and U-2s.",
    },
    {
        "name": "Camp Arifjan",
        "lat": 28.9308,
        "lon": 48.0831,
        "type": "garrison",
        "country": "Kuwait",
        "operator": "US",
        "notes": "US Army Central (ARCENT) forward HQ. Largest US Army base in the Gulf.",
    },
    {
        "name": "Ali Al Salem Air Base",
        "lat": 29.3467,
        "lon": 47.5208,
        "type": "airbase",
        "country": "Kuwait",
        "operator": "US",
        "notes": "Major transit hub for US forces entering/exiting theater.",
    },
    {
        "name": "Naval Support Activity Bahrain",
        "lat": 26.2361,
        "lon": 50.6056,
        "type": "naval_base",
        "country": "Bahrain",
        "operator": "US",
        "notes": "Headquarters of US Naval Forces Central Command / US 5th Fleet.",
    },
    {
        "name": "Camp Lemonnier",
        "lat": 11.5469,
        "lon": 43.1558,
        "type": "garrison",
        "country": "Djibouti",
        "operator": "US",
        "notes": "Only permanent US military base in Africa. CJTF-HOA. Drone ops hub.",
    },
    {
        "name": "Al-Tanf Garrison",
        "lat": 33.5103,
        "lon": 38.6600,
        "type": "garrison",
        "country": "Syria",
        "operator": "US",
        "notes": "US outpost at the Iraq-Jordan-Syria tri-border. 55 km deconfliction zone.",
    },
    {
        "name": "Incirlik Air Base",
        "lat": 37.0021,
        "lon": 35.4259,
        "type": "airbase",
        "country": "Turkey",
        "operator": "US",
        "notes": "Key NATO / USAF base. Rumored nuclear weapons storage (WS3 vaults).",
    },
    {
        "name": "Camp Buehring",
        "lat": 29.3614,
        "lon": 47.6614,
        "type": "garrison",
        "country": "Kuwait",
        "operator": "US",
        "notes": "US Army staging / training area in northern Kuwait.",
    },
    {
        "name": "Al Asad Air Base",
        "lat": 33.7856,
        "lon": 42.4431,
        "type": "airbase",
        "country": "Iraq",
        "operator": "US",
        "notes": "Major US air base in Anbar province, Iraq. Targeted by Iran in Jan 2020.",
    },
    {
        "name": "Erbil Air Base",
        "lat": 36.2376,
        "lon": 43.9632,
        "type": "airbase",
        "country": "Iraq",
        "operator": "US",
        "notes": "US / Coalition presence at Erbil International Airport, Kurdistan Region.",
    },
    {
        "name": "Muwaffaq Salti Air Base",
        "lat": 32.3564,
        "lon": 36.7822,
        "type": "airbase",
        "country": "Jordan",
        "operator": "US",
        "notes": "USAF drone operations base in eastern Jordan. Key ISR hub.",
    },
    {
        "name": "Tower 22",
        "lat": 33.3720,
        "lon": 38.7970,
        "type": "garrison",
        "country": "Jordan",
        "operator": "US",
        "notes": "US logistics post near Rukban on the Jordan-Syria border. Attacked Jan 2024.",
    },

    # -----------------------------------------------------------------------
    # UNITED KINGDOM
    # -----------------------------------------------------------------------
    {
        "name": "RAF Akrotiri",
        "lat": 34.5908,
        "lon": 32.9879,
        "type": "airbase",
        "country": "Cyprus",
        "operator": "UK",
        "notes": "UK Sovereign Base Area. Typhoons, P-8 Poseidon, Voyager tankers. Signals intelligence.",
    },
    {
        "name": "Duqm Naval Facility",
        "lat": 19.6705,
        "lon": 57.7007,
        "type": "naval_base",
        "country": "Oman",
        "operator": "UK",
        "notes": "UK Joint Logistics Support Base. Can berth Queen Elizabeth-class carriers.",
    },
    {
        "name": "HMS Juffair (UK NSF Bahrain)",
        "lat": 26.2175,
        "lon": 50.6097,
        "type": "naval_base",
        "country": "Bahrain",
        "operator": "UK",
        "notes": "UK Naval Support Facility. Base for mine countermeasures and patrol ships.",
    },

    # -----------------------------------------------------------------------
    # FRANCE
    # -----------------------------------------------------------------------
    {
        "name": "French Naval Base Abu Dhabi",
        "lat": 24.5330,
        "lon": 54.6517,
        "type": "naval_base",
        "country": "UAE",
        "operator": "France",
        "notes": "Peace Camp / Camp de la Paix. French Forces in the UAE. Rafale fighters.",
    },
    {
        "name": "French Forces Djibouti (FFDj)",
        "lat": 11.5500,
        "lon": 43.1500,
        "type": "garrison",
        "country": "Djibouti",
        "operator": "France",
        "notes": "Largest French overseas military base (~1,500 troops). Air, land, and naval assets.",
    },
    {
        "name": "French Air Base 104 Al Dhafra",
        "lat": 24.2450,
        "lon": 54.5500,
        "type": "airbase",
        "country": "UAE",
        "operator": "France",
        "notes": "French Air & Space Force detachment. Rafale fighter deployments.",
    },

    # -----------------------------------------------------------------------
    # RUSSIA
    # -----------------------------------------------------------------------
    {
        "name": "Khmeimim Air Base",
        "lat": 35.4011,
        "lon": 35.9487,
        "type": "airbase",
        "country": "Syria",
        "operator": "Russia",
        "notes": "Main Russian air base in Syria (Latakia). Su-35, Su-34, Su-24, S-400 SAM.",
    },
    {
        "name": "Tartus Naval Facility",
        "lat": 34.8901,
        "lon": 35.8866,
        "type": "naval_base",
        "country": "Syria",
        "operator": "Russia",
        "notes": "Russian Navy PMTO. Only Mediterranean warm-water port. Being expanded.",
    },

    # -----------------------------------------------------------------------
    # ISRAEL
    # -----------------------------------------------------------------------
    {
        "name": "Nevatim Air Base",
        "lat": 31.2083,
        "lon": 34.9333,
        "type": "airbase",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Home of Israeli F-35I Adir fleet. 116 and 140 Squadrons.",
    },
    {
        "name": "Ramon Air Base",
        "lat": 30.7761,
        "lon": 34.6681,
        "type": "airbase",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Southern Negev. F-16I Sufa. Advanced pilot training.",
    },
    {
        "name": "Hatzerim Air Base",
        "lat": 31.2344,
        "lon": 34.6636,
        "type": "airbase",
        "country": "Israel",
        "operator": "Israel",
        "notes": "IAF Flight Academy. Home of aerobatic team. F-16C/D units.",
    },
    {
        "name": "Palmachim Air Base",
        "lat": 31.8980,
        "lon": 34.6908,
        "type": "airbase",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Missile test range. Jericho ballistic missiles. Arrow & David's Sling tests.",
    },
    {
        "name": "Haifa Naval Base",
        "lat": 32.8197,
        "lon": 34.9892,
        "type": "naval_base",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Israel Navy primary base. Dolphin-class submarines, Sa'ar corvettes.",
    },
    {
        "name": "Ramat David Air Base",
        "lat": 32.6653,
        "lon": 35.1797,
        "type": "airbase",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Northern Israel. F-16 and helicopter units. Key for Lebanon ops.",
    },
    {
        "name": "Tel Nof Air Base",
        "lat": 31.8394,
        "lon": 34.8222,
        "type": "airbase",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Strategic transport, heavy helicopter (CH-53), Boeing 707 tankers.",
    },
    {
        "name": "Sdot Micha",
        "lat": 31.7000,
        "lon": 35.0000,
        "type": "missile_site",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Reportedly houses Jericho II/III nuclear-capable ballistic missiles.",
    },
    {
        "name": "Dimona (Negev Nuclear Research Center)",
        "lat": 31.0014,
        "lon": 35.1447,
        "type": "nuclear",
        "country": "Israel",
        "operator": "Israel",
        "notes": "Israeli nuclear reactor complex. Restricted airspace. Undeclared program.",
    },

    # -----------------------------------------------------------------------
    # IRAN
    # -----------------------------------------------------------------------
    {
        "name": "Isfahan Nuclear Technology Center",
        "lat": 32.7553,
        "lon": 51.7286,
        "type": "nuclear",
        "country": "Iran",
        "operator": "Iran",
        "notes": "Uranium conversion facility (UCF). Produces UF6 feedstock for enrichment.",
    },
    {
        "name": "Natanz Enrichment Facility",
        "lat": 33.7244,
        "lon": 51.7269,
        "type": "nuclear",
        "country": "Iran",
        "operator": "Iran",
        "notes": "Primary uranium enrichment plant. Deep underground halls. Stuxnet target.",
    },
    {
        "name": "Fordow Enrichment Facility",
        "lat": 34.8808,
        "lon": 51.1219,
        "type": "nuclear",
        "country": "Iran",
        "operator": "Iran",
        "notes": "Deeply buried enrichment site inside a mountain near Qom.",
    },
    {
        "name": "Bushehr Nuclear Power Plant",
        "lat": 28.8331,
        "lon": 50.8861,
        "type": "nuclear",
        "country": "Iran",
        "operator": "Iran",
        "notes": "Iran's only operational nuclear power plant. Russian-built VVER-1000.",
    },
    {
        "name": "Bandar Abbas Naval Base",
        "lat": 27.1486,
        "lon": 56.2789,
        "type": "naval_base",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIN main base. Strait of Hormuz control. Kilo-class submarines.",
    },
    {
        "name": "Chabahar Naval Base",
        "lat": 25.2919,
        "lon": 60.6428,
        "type": "naval_base",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIN base on Gulf of Oman. Outside Strait of Hormuz. Deep-water port.",
    },
    {
        "name": "Khatam al-Anbiya Air Defense HQ",
        "lat": 35.7000,
        "lon": 51.4000,
        "type": "air_defense",
        "country": "Iran",
        "operator": "Iran",
        "notes": "Central HQ of Iran's integrated air defense network. Tehran.",
    },
    {
        "name": "Isfahan / Khatami Air Base (TFB.8)",
        "lat": 32.7508,
        "lon": 51.8631,
        "type": "airbase",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIAF Tactical Fighter Base 8. F-14 Tomcats. Nearby nuclear facilities.",
    },
    {
        "name": "Mehrabad Air Base / TFB.1",
        "lat": 35.6892,
        "lon": 51.3117,
        "type": "airbase",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIAF base co-located with Tehran Mehrabad airport. VIP transport.",
    },
    {
        "name": "Shiraz / TFB.7",
        "lat": 29.5422,
        "lon": 52.5900,
        "type": "airbase",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIAF Tactical Fighter Base 7. F-14 Tomcats and F-4 Phantoms.",
    },
    {
        "name": "Tabriz / TFB.2",
        "lat": 38.1339,
        "lon": 46.2350,
        "type": "airbase",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIAF Tactical Fighter Base 2. F-5 Tigers. Northwestern Iran.",
    },
    {
        "name": "Bushehr / TFB.6",
        "lat": 28.9486,
        "lon": 50.8350,
        "type": "airbase",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIAF Tactical Fighter Base 6. F-4 Phantoms. Guards nuclear plant.",
    },
    {
        "name": "Bandar Abbas / TFB.9",
        "lat": 27.2183,
        "lon": 56.1778,
        "type": "airbase",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRIAF base guarding Strait of Hormuz approaches.",
    },
    {
        "name": "Parchin Military Complex",
        "lat": 35.5300,
        "lon": 51.7700,
        "type": "missile_site",
        "country": "Iran",
        "operator": "Iran",
        "notes": "Weapons R&D. Suspected explosive testing for nuclear weapon designs.",
    },
    {
        "name": "Imam Ali Missile Base",
        "lat": 34.8500,
        "lon": 47.0700,
        "type": "missile_site",
        "country": "Iran",
        "operator": "Iran",
        "notes": "IRGC ballistic missile garrison. Underground storage tunnels.",
    },

    # -----------------------------------------------------------------------
    # SAUDI ARABIA
    # -----------------------------------------------------------------------
    {
        "name": "King Abdulaziz Air Base (Dhahran)",
        "lat": 26.2653,
        "lon": 50.1522,
        "type": "airbase",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "RSAF Eastern Sector HQ. Patriot batteries. Near Aramco HQ.",
    },
    {
        "name": "King Faisal Air Base (Tabuk)",
        "lat": 28.3656,
        "lon": 36.6189,
        "type": "airbase",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "RSAF Northwestern sector. Typhoon and F-15SA. Near Israel / Jordan.",
    },
    {
        "name": "Prince Sultan Air Base (PSAB)",
        "lat": 24.0627,
        "lon": 47.5802,
        "type": "airbase",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "Al Kharj. USAF returned 2019. THAAD, Patriot. Key to defense of Riyadh.",
    },
    {
        "name": "King Khalid Air Base (Khamis Mushait)",
        "lat": 18.2972,
        "lon": 42.8044,
        "type": "airbase",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "RSAF Southern sector. F-15 ops against Houthis. Near Yemen border.",
    },
    {
        "name": "King Fahd Naval Base (Jubail)",
        "lat": 27.0508,
        "lon": 49.6478,
        "type": "naval_base",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "Royal Saudi Naval Forces Eastern Fleet HQ. Al Jubail.",
    },
    {
        "name": "Jeddah Naval Base",
        "lat": 21.4367,
        "lon": 39.1678,
        "type": "naval_base",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "Royal Saudi Naval Forces Western Fleet HQ. Red Sea.",
    },
    {
        "name": "Ras Tanura Refinery / Terminal",
        "lat": 26.6350,
        "lon": 50.0608,
        "type": "refinery",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "Saudi Aramco. One of the world's largest oil export terminals.",
    },
    {
        "name": "Abqaiq Oil Processing Facility",
        "lat": 25.9375,
        "lon": 49.6811,
        "type": "refinery",
        "country": "Saudi Arabia",
        "operator": "Saudi",
        "notes": "World's largest oil stabilization plant. Attacked by drones/cruise missiles Sep 2019.",
    },

    # -----------------------------------------------------------------------
    # TURKEY
    # -----------------------------------------------------------------------
    # Incirlik is listed under US above
    {
        "name": "Diyarbakir Air Base",
        "lat": 37.8939,
        "lon": 40.2100,
        "type": "airbase",
        "country": "Turkey",
        "operator": "Turkey",
        "notes": "TurAF 8th Main Jet Base. F-16 units. Key for southeastern operations.",
    },
    {
        "name": "Batman Air Base",
        "lat": 37.9289,
        "lon": 41.1186,
        "type": "airbase",
        "country": "Turkey",
        "operator": "Turkey",
        "notes": "TurAF forward operating base. Used for cross-border ops into Iraq.",
    },
    {
        "name": "Konya Air Base",
        "lat": 37.9792,
        "lon": 32.5617,
        "type": "airbase",
        "country": "Turkey",
        "operator": "Turkey",
        "notes": "TurAF 3rd Main Jet Base. NATO E-3 AWACS forward operating location.",
    },
    {
        "name": "Kurecik Radar Station",
        "lat": 38.6553,
        "lon": 38.4586,
        "type": "radar",
        "country": "Turkey",
        "operator": "US",
        "notes": "AN/TPY-2 radar (NATO BMD). Tracks Iranian ballistic missile launches.",
    },

    # -----------------------------------------------------------------------
    # UAE
    # -----------------------------------------------------------------------
    {
        "name": "Al Minhad Air Base",
        "lat": 25.0274,
        "lon": 55.3659,
        "type": "airbase",
        "country": "UAE",
        "operator": "UAE",
        "notes": "UAE Air Force & Air Defence. Also hosts Australian, Canadian contingents.",
    },
    {
        "name": "Jebel Ali Port / Naval Base",
        "lat": 25.0117,
        "lon": 55.0600,
        "type": "naval_base",
        "country": "UAE",
        "operator": "UAE",
        "notes": "World's largest man-made port. US Navy frequent port call site.",
    },
    {
        "name": "Fujairah Oil Terminal",
        "lat": 25.1164,
        "lon": 56.3558,
        "type": "port",
        "country": "UAE",
        "operator": "UAE",
        "notes": "Strategic oil storage and bunkering hub outside Strait of Hormuz.",
    },

    # -----------------------------------------------------------------------
    # QATAR
    # -----------------------------------------------------------------------
    {
        "name": "Tamim Air Base (Doha)",
        "lat": 25.1139,
        "lon": 51.2314,
        "type": "airbase",
        "country": "Qatar",
        "operator": "Qatar",
        "notes": "Qatar Emiri Air Force primary base. Rafale and Typhoon jets.",
    },

    # -----------------------------------------------------------------------
    # KUWAIT
    # -----------------------------------------------------------------------
    {
        "name": "Ahmed Al Jaber Air Base",
        "lat": 28.9347,
        "lon": 47.7919,
        "type": "airbase",
        "country": "Kuwait",
        "operator": "Kuwait",
        "notes": "Kuwait Air Force base. F/A-18 Hornets. Southern Kuwait.",
    },

    # -----------------------------------------------------------------------
    # BAHRAIN
    # -----------------------------------------------------------------------
    {
        "name": "Shaikh Isa Air Base",
        "lat": 25.9183,
        "lon": 50.5906,
        "type": "airbase",
        "country": "Bahrain",
        "operator": "Bahrain",
        "notes": "Royal Bahraini Air Force. F-16 Block 70 incoming. Also US Marine AV-8B.",
    },

    # -----------------------------------------------------------------------
    # OMAN
    # -----------------------------------------------------------------------
    {
        "name": "Thumrait Air Base",
        "lat": 17.6660,
        "lon": 53.9342,
        "type": "airbase",
        "country": "Oman",
        "operator": "Oman",
        "notes": "Royal Air Force of Oman. USAF has access agreement. Southern Oman.",
    },
    {
        "name": "Muscat / Al Musannah Air Base",
        "lat": 23.6408,
        "lon": 57.4936,
        "type": "airbase",
        "country": "Oman",
        "operator": "Oman",
        "notes": "RAFO main operating base. F-16, Typhoon orders.",
    },
    {
        "name": "Masirah Island Air Base",
        "lat": 20.6754,
        "lon": 58.8906,
        "type": "airbase",
        "country": "Oman",
        "operator": "Oman",
        "notes": "Strategic island base. USAF access. Arabian Sea surveillance.",
    },

    # -----------------------------------------------------------------------
    # JORDAN
    # -----------------------------------------------------------------------
    {
        "name": "King Abdullah II Air Base (Marka)",
        "lat": 31.9722,
        "lon": 35.9933,
        "type": "airbase",
        "country": "Jordan",
        "operator": "Jordan",
        "notes": "RJAF primary base at Amman. F-16 MLU.",
    },
    {
        "name": "Prince Hassan Air Base",
        "lat": 32.1614,
        "lon": 37.1492,
        "type": "airbase",
        "country": "Jordan",
        "operator": "Jordan",
        "notes": "RJAF H-5 base in eastern desert. F-16 ops.",
    },
    {
        "name": "Aqaba Port",
        "lat": 29.5167,
        "lon": 35.0000,
        "type": "port",
        "country": "Jordan",
        "operator": "Jordan",
        "notes": "Jordan's only seaport. Strategic logistics node at Red Sea.",
    },

    # -----------------------------------------------------------------------
    # EGYPT
    # -----------------------------------------------------------------------
    {
        "name": "Cairo West Air Base",
        "lat": 30.1164,
        "lon": 30.9153,
        "type": "airbase",
        "country": "Egypt",
        "operator": "Egypt",
        "notes": "EAF main strike base. F-16, Rafale. Largest Egyptian air base.",
    },
    {
        "name": "Berenice Military Base",
        "lat": 23.9425,
        "lon": 35.4450,
        "type": "airbase",
        "country": "Egypt",
        "operator": "Egypt",
        "notes": "New military mega-base on Red Sea coast. Opened 2020. Naval & air.",
    },
    {
        "name": "Ras Banas Air Base",
        "lat": 23.5842,
        "lon": 35.7803,
        "type": "airbase",
        "country": "Egypt",
        "operator": "Egypt",
        "notes": "Red Sea coast base near Berenice. Potential US access.",
    },
    {
        "name": "Mohamed Naguib Military Base",
        "lat": 30.8500,
        "lon": 28.2500,
        "type": "garrison",
        "country": "Egypt",
        "operator": "Egypt",
        "notes": "Largest military base in Middle East / Africa. Near El Alamein.",
    },
    {
        "name": "Alexandria Naval Base",
        "lat": 31.1897,
        "lon": 29.8678,
        "type": "naval_base",
        "country": "Egypt",
        "operator": "Egypt",
        "notes": "Egyptian Navy Mediterranean Fleet HQ. FREMM frigates.",
    },
    {
        "name": "Suez Canal / Port Said",
        "lat": 31.2653,
        "lon": 32.3019,
        "type": "port",
        "country": "Egypt",
        "operator": "Egypt",
        "notes": "Northern entrance to the Suez Canal. Strategic chokepoint.",
    },

    # -----------------------------------------------------------------------
    # IRAQ
    # -----------------------------------------------------------------------
    {
        "name": "Baghdad International Airport / Victory Base",
        "lat": 33.2625,
        "lon": 44.2346,
        "type": "airbase",
        "country": "Iraq",
        "operator": "Iraq",
        "notes": "Iraqi Air Force ops. Former US Camp Victory complex adjacent.",
    },
    {
        "name": "Balad Air Base (Al Bakr)",
        "lat": 33.9403,
        "lon": 44.3614,
        "type": "airbase",
        "country": "Iraq",
        "operator": "Iraq",
        "notes": "Former US mega-base. Now Iraqi F-16 operations. North of Baghdad.",
    },
    {
        "name": "Basra International / Shuaiba",
        "lat": 30.5494,
        "lon": 47.6621,
        "type": "airbase",
        "country": "Iraq",
        "operator": "Iraq",
        "notes": "Southern Iraq. Iraqi Army aviation. Near Iran border.",
    },
    {
        "name": "Umm Qasr Naval Base",
        "lat": 30.0350,
        "lon": 47.9308,
        "type": "naval_base",
        "country": "Iraq",
        "operator": "Iraq",
        "notes": "Iraq's only deep-water port. Iraqi Navy HQ.",
    },

    # -----------------------------------------------------------------------
    # DJIBOUTI (non-US/France)
    # -----------------------------------------------------------------------
    {
        "name": "PLA Support Base Djibouti",
        "lat": 11.5917,
        "lon": 43.0708,
        "type": "naval_base",
        "country": "Djibouti",
        "operator": "China",
        "notes": "China's first overseas military base. Opened 2017. Adjacent to Camp Lemonnier.",
    },
    {
        "name": "Japanese Self-Defense Force Base Djibouti",
        "lat": 11.5556,
        "lon": 43.1500,
        "type": "garrison",
        "country": "Djibouti",
        "operator": "Japan",
        "notes": "JSDF counter-piracy base. Small air detachment.",
    },

    # -----------------------------------------------------------------------
    # YEMEN (Houthi targets / coalition)
    # -----------------------------------------------------------------------
    {
        "name": "Al Anad Air Base",
        "lat": 13.1750,
        "lon": 44.7717,
        "type": "airbase",
        "country": "Yemen",
        "operator": "Yemen",
        "notes": "Former US drone base. Largest Yemeni air base. Contested in civil war.",
    },
    {
        "name": "Hodeidah Port",
        "lat": 14.7981,
        "lon": 42.9536,
        "type": "port",
        "country": "Yemen",
        "operator": "Yemen",
        "notes": "Critical Red Sea port. Houthi-controlled. Key humanitarian and smuggling node.",
    },
]


# ---------------------------------------------------------------------------
# Operator -> KML icon color (aabbggrr KML format)
# ---------------------------------------------------------------------------
OPERATOR_COLORS = {
    "US":       "ffff5500",   # blue (KML is AABBGGRR, so ff0055ff -> orange, ffff5500 -> blue-ish)
    "UK":       "ffff00aa",   # purple
    "France":   "ffffff00",   # cyan-ish
    "Russia":   "ff0000ff",   # red
    "Iran":     "ff00aa00",   # green
    "Israel":   "ffffffff",   # white
    "Saudi":    "ff00ccff",   # orange-yellow
    "UAE":      "ff88cc00",   # teal
    "Turkey":   "ff3366ff",   # salmon/red-orange
    "Qatar":    "ff9966cc",   # mauve
    "Kuwait":   "ff66cccc",   # sand
    "Bahrain":  "ff6699ff",   # peach
    "Oman":     "ff339966",   # dark teal
    "Jordan":   "ff6666cc",   # muted red
    "Egypt":    "ff0066cc",   # dark orange
    "Iraq":     "ff669999",   # grey-green
    "China":    "ff0000cc",   # dark red
    "Japan":    "ffcc6699",   # pink
    "Yemen":    "ff99cccc",   # tan
    "Djibouti": "ff999999",   # grey
}

# Readable color for the popup header per operator
OPERATOR_HEADER_COLOR = {
    "US":       "#0055ff",
    "UK":       "#aa00ff",
    "France":   "#00cccc",
    "Russia":   "#ff0000",
    "Iran":     "#00cc00",
    "Israel":   "#ffffff",
    "Saudi":    "#ffcc00",
    "UAE":      "#00cc88",
    "Turkey":   "#ff6633",
    "Qatar":    "#cc6699",
    "Kuwait":   "#cccc66",
    "Bahrain":  "#ff9966",
    "Oman":     "#669933",
    "Jordan":   "#cc6666",
    "Egypt":    "#cc6600",
    "Iraq":     "#999966",
    "China":    "#cc0000",
    "Japan":    "#9966cc",
    "Yemen":    "#cccc99",
    "Djibouti": "#999999",
}

# Type -> icon URL
TYPE_ICONS = {
    "airbase":      "http://maps.google.com/mapfiles/kml/shapes/airports.png",
    "naval_base":   "http://maps.google.com/mapfiles/kml/shapes/marina.png",
    "port":         "http://maps.google.com/mapfiles/kml/shapes/ports.png",
    "garrison":     "http://maps.google.com/mapfiles/kml/shapes/ranger_station.png",
    "radar":        "http://maps.google.com/mapfiles/kml/shapes/electronics.png",
    "missile_site": "http://maps.google.com/mapfiles/kml/shapes/target.png",
    "nuclear":      "http://maps.google.com/mapfiles/kml/shapes/caution.png",
    "refinery":     "http://maps.google.com/mapfiles/kml/shapes/firedept.png",
    "air_defense":  "http://maps.google.com/mapfiles/kml/shapes/shield.png",
    "drone_base":   "http://maps.google.com/mapfiles/kml/shapes/heliport.png",
    "logistics":    "http://maps.google.com/mapfiles/kml/shapes/truck.png",
}
DEFAULT_ICON = "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"


# ---------------------------------------------------------------------------
# KML generation
# ---------------------------------------------------------------------------

def _build_description(base: dict) -> str:
    """Build an HTML popup matching the project's dark-theme style."""
    op = base["operator"]
    hdr_color = OPERATOR_HEADER_COLOR.get(op, "#cccccc")
    return f"""<![CDATA[
<style>
  table {{ font-family: Arial; font-size: 13px; border-collapse: collapse; width: 100%; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #444; }}
  .label {{ color: #aaa; font-weight: bold; white-space: nowrap; }}
  .val {{ color: #fff; }}
  .header {{ background: {hdr_color}; color: white; padding: 6px 8px; font-size: 14px; font-weight: bold; }}
</style>
<div style="background:#1a1a1a; border-radius:6px; overflow:hidden; min-width:280px;">
  <div class="header">{saxutils.escape(base['name'])}</div>
  <table>
    <tr><td class="label">Type</td><td class="val">{saxutils.escape(base['type'].replace('_', ' ').title())}</td></tr>
    <tr><td class="label">Country</td><td class="val">{saxutils.escape(base['country'])}</td></tr>
    <tr><td class="label">Operator</td><td class="val" style="color:{hdr_color}">{saxutils.escape(op)}</td></tr>
    <tr><td class="label">Position</td><td class="val">{base['lat']:.4f}, {base['lon']:.4f}</td></tr>
    <tr><td class="label">Notes</td><td class="val">{saxutils.escape(base['notes'])}</td></tr>
  </table>
</div>
]]>"""


def generate_bases_kml(output_path: str = None) -> str:
    """
    Write a Bases.kml file with all known facilities, organised into
    KML folders by country and colour-coded by operator.

    Returns the output file path.
    """
    if output_path is None:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Bases.kml")

    # Group by country
    by_country: dict[str, list[dict]] = {}
    for b in BASES:
        by_country.setdefault(b["country"], []).append(b)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<Document>',
        '<name>Military Bases &amp; Key Infrastructure - ME OSINT</name>',
    ]

    for country in sorted(by_country):
        lines.append(f'<Folder><name>{saxutils.escape(country)}</name>')
        for base in sorted(by_country[country], key=lambda b: b["name"]):
            op = base["operator"]
            color = OPERATOR_COLORS.get(op, "ff00ffff")
            icon = TYPE_ICONS.get(base["type"], DEFAULT_ICON)

            lines.append('<Placemark>')
            lines.append(f'  <name>{saxutils.escape(base["name"])}</name>')
            lines.append(f'  <description>{_build_description(base)}</description>')
            lines.append('  <Style>')
            lines.append('    <IconStyle>')
            lines.append(f'      <color>{color}</color>')
            lines.append('      <scale>1.1</scale>')
            lines.append(f'      <Icon><href>{icon}</href></Icon>')
            lines.append('    </IconStyle>')
            lines.append('    <BalloonStyle><bgColor>ff1a1a1a</bgColor><textColor>ffffffff</textColor></BalloonStyle>')
            lines.append('  </Style>')
            lines.append(f'  <Point><coordinates>{base["lon"]},{base["lat"]},0</coordinates></Point>')
            lines.append('</Placemark>')
        lines.append('</Folder>')

    lines.append('</Document>')
    lines.append('</kml>')

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return output_path


# ---------------------------------------------------------------------------
# Nearest-base lookup (for correlation engine)
# ---------------------------------------------------------------------------

def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in nautical miles."""
    R_NM = 3440.065  # Earth radius in NM
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * R_NM * math.asin(math.sqrt(a))


def get_nearest_base(lat: float, lon: float, max_nm: float = 100) -> dict | None:
    """
    Return the nearest known base to a given coordinate, or None if nothing
    is within *max_nm* nautical miles.

    The returned dict is a copy of the base entry with an extra 'distance_nm'
    field.
    """
    best = None
    best_dist = float("inf")
    for base in BASES:
        d = _haversine_nm(lat, lon, base["lat"], base["lon"])
        if d < best_dist:
            best_dist = d
            best = base
    if best is None or best_dist > max_nm:
        return None
    result = dict(best)
    result["distance_nm"] = round(best_dist, 1)
    return result


def get_bases_in_range(lat: float, lon: float, max_nm: float = 100) -> list[dict]:
    """Return *all* bases within max_nm of a coordinate, sorted by distance."""
    results = []
    for base in BASES:
        d = _haversine_nm(lat, lon, base["lat"], base["lon"])
        if d <= max_nm:
            entry = dict(base)
            entry["distance_nm"] = round(d, 1)
            results.append(entry)
    return sorted(results, key=lambda x: x["distance_nm"])


# ---------------------------------------------------------------------------
# JSON export (for web dashboard)
# ---------------------------------------------------------------------------

def get_bases_json() -> list[dict]:
    """Return all bases as a list of dicts (JSON-serialisable)."""
    return [dict(b) for b in BASES]


def get_bases_geojson() -> dict:
    """Return all bases as a GeoJSON FeatureCollection."""
    features = []
    for b in BASES:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [b["lon"], b["lat"]],
            },
            "properties": {
                "name": b["name"],
                "type": b["type"],
                "country": b["country"],
                "operator": b["operator"],
                "notes": b["notes"],
                "icon_color": OPERATOR_HEADER_COLOR.get(b["operator"], "#cccccc"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    path = generate_bases_kml()
    print(f"Wrote {len(BASES)} facilities to {path}")

    # Quick sanity check: nearest base to downtown Doha
    test = get_nearest_base(25.2854, 51.5310)
    if test:
        print(f"Nearest base to Doha center: {test['name']} ({test['distance_nm']} nm)")

    # JSON count
    print(f"JSON export: {len(get_bases_json())} entries")
