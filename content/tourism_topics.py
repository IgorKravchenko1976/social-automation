"""Content directions and RSS sources for tourism posts.

Instead of fixed topic lists, we define broad DIRECTIONS (categories).
AI generates a unique specific topic within a chosen direction,
checking against recent post history to avoid repetition.
"""

TOURISM_RSS_FEEDS = [
    # Ukrainian sources first (priority)
    ("Visit Ukraine", "https://visitukraine.today/blog/rss"),
    ("Укрінформ Туризм", "https://www.ukrinform.ua/rss/rubric-tourism"),
    # International travel-only sources
    ("The Guardian Travel", "https://www.theguardian.com/travel/rss"),
    ("Euronews Travel", "https://www.euronews.com/travel/travel-news/rss"),
    ("Lonely Planet", "https://www.lonelyplanet.com/news/feed"),
    ("Condé Nast Traveler", "https://www.cntraveler.com/feed/rss"),
    ("Travel + Leisure", "https://www.travelandleisure.com/arcio/rss/"),
    ("CNN Travel", "https://rss.cnn.com/rss/edition_travel.rss"),
]

BANNED_RSS_KEYWORDS = [
    "russia", "росія", "російськ", "москв", "putin", "путін",
    "trump", "трамп", "iran", "іран", "war", "війн", "military",
    "missile", "ракет", "sanction", "санкці", "nato", "нато",
    "nuclear", "ядер", "bomb", "election", "вибор", "congress",
    "конгрес", "senate", "сенат", "pentagon", "пентагон",
    "weapon", "зброя", "invasion", "вторгн", "kremlin", "кремл",
    "pearl harbor", "attack on iran",
]

# ── Directions for AI topic generation ───────────────────────────────────

ACTIVE_DIRECTIONS = [
    "Гірськолижні курорти світу та України",
    "Серфінг-спотти та пляжний спорт",
    "Марафони, ультрамарафони та забіги",
    "Формула 1, MotoGP та автоспорт",
    "Дайвінг та снорклінг",
    "Велотури та велогонки",
    "Трекінг, альпінізм та гірські маршрути",
    "Водні види спорту: каякінг, рафтинг, вітрильний спорт",
    "Тенісні турніри та корти",
    "Футбольні стадіони та матчі",
    "Гольф-поля та турніри",
    "Активний відпочинок в Карпатах та Україні",
    "Парапланеризм, банджі, зіплайн та екстрим",
    "Кінний спорт та кінні тури",
    "Зимові види спорту: сноуборд, біатлон, ковзани",
    "Йога-ретріти та wellness-подорожі",
]

LEISURE_DIRECTIONS = [
    "Історичні міста Європи",
    "Азійські міста та культура",
    "Острівний відпочинок",
    "Гастротуризм: кухні народів світу",
    "Природні чудеса та національні парки",
    "Музеї та галереї мистецтва",
    "Гірські курорти та долини",
    "Морські та річкові круїзи",
    "Вуличні ринки, базари та шопінг",
    "Річкові подорожі та водні маршрути",
    "Українські міста для подорожей",
    "Природа та заповідники України",
    "Гастротуризм Україною",
    "Замки, фортеці та палаци",
    "Романтичні місця для пар",
    "Країни Латинської Америки",
    "Африканські подорожі та сафарі",
    "Скандинавські країни",
    "Середземноморське узбережжя",
    "Столиці та мегаполіси світу",
]

FEATURE_DIRECTIONS = [
    "Карта, маркери подій та геолокація",
    "Профіль, приватність та налаштування",
    "Фото, відео та мультимедіа",
    "Спілкування, коментарі та друзі",
    "Реєстрація, вхід та безпека",
    "Пошук, фільтри та радіус",
    "Режими перегляду: 2D, 3D, авто",
    "Push-сповіщення та оновлення",
    "Мовна підтримка та локалізація",
    "Офлайн-режим та черга завантаження",
]
