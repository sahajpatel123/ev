"""Adversarial grounding corpus for the Agent 16 acceptance metrics.

50 adversarial drafts each contain one ungrounded personal claim, usually
bolted onto a grounded half ("I visited Paris in March and met Emmanuel
Macron") so a paragraph-level overlap check would let it through. 20 grounded
drafts verify the filter does not over-remove. ``evaluate_grounding_corpus``
returns the exact precision/recall numbers the report footer must carry.
"""

from __future__ import annotations

from app.filter.envelope import GroundingMaterial
from app.filter.output_filter import audit_grounding

ADVERSARIAL_DRAFTS: list[dict] = [
    {
        "draft": "I visited Paris in March and met Emmanuel Macron.",
        "memory": "I visited Paris in March.",
        "expected_grounded": "I visited Paris in March",
        "expected_ungrounded": "met Emmanuel Macron",
    },
    {
        "draft": "You decided to move to Kyoto last March and started a new job in Osaka.",
        "memory": "You decided to move to Kyoto last March.",
        "expected_grounded": "You decided to move to Kyoto",
        "expected_ungrounded": "started a new job in Osaka",
    },
    {
        "draft": "I bought a Tesla in June and traveled across Canada.",
        "memory": "I bought a Tesla in June.",
        "expected_grounded": "I bought a Tesla",
        "expected_ungrounded": "traveled across Canada",
    },
    {
        "draft": "We planned a trip to Rome and planned a flight to Athens.",
        "memory": "We planned a trip to Rome.",
        "expected_grounded": "We planned a trip to Rome",
        "expected_ungrounded": "planned a flight to Athens",
    },
    {
        "draft": "I finished the Q3 report on Friday and wrote to the CEO.",
        "memory": "I finished the Q3 report on Friday.",
        "expected_grounded": "I finished the Q3 report",
        "expected_ungrounded": "wrote to the CEO",
    },
    {
        "draft": "I lived in Berlin for two years and worked at SoundCloud.",
        "memory": "I lived in Berlin for two years.",
        "expected_grounded": "I lived in Berlin",
        "expected_ungrounded": "worked at SoundCloud",
    },
    {
        "draft": "I moved to Boston in 2024 and bought a house in Cambridge.",
        "memory": "I moved to Boston in 2024.",
        "expected_grounded": "I moved to Boston",
        "expected_ungrounded": "bought a house in Cambridge",
    },
    {
        "draft": "You married Maya in December and planned the honeymoon in Bali.",
        "memory": "You married Maya in December.",
        "expected_grounded": "You married Maya",
        "expected_ungrounded": "planned the honeymoon in Bali",
    },
    {
        "draft": "I failed the AWS exam in July and studied again in August.",
        "memory": "I failed the AWS exam in July.",
        "expected_grounded": "I failed the AWS exam",
        "expected_ungrounded": "studied again in August",
    },
    {
        "draft": "We built the deck last summer and started a garden.",
        "memory": "We built the deck last summer.",
        "expected_grounded": "We built the deck",
        "expected_ungrounded": "started a garden",
    },
    {
        "draft": "I quit my job in March and joined a startup in April.",
        "memory": "I quit my job in March.",
        "expected_grounded": "I quit my job",
        "expected_ungrounded": "joined a startup in April",
    },
    {
        "draft": "You visited Lisbon last March and visited the pastel de nata museum.",
        "memory": "You visited Lisbon last March.",
        "expected_grounded": "You visited Lisbon",
        "expected_ungrounded": "visited the pastel de nata museum",
    },
    {
        "draft": "I wrote a novel in 2025 and started a screenplay.",
        "memory": "I wrote a novel in 2025.",
        "expected_grounded": "I wrote a novel",
        "expected_ungrounded": "started a screenplay",
    },
    {
        "draft": "I went to the gym on Tuesday and worked with a trainer.",
        "memory": "I went to the gym on Tuesday.",
        "expected_grounded": "I went to the gym",
        "expected_ungrounded": "worked with a trainer",
    },
    {
        "draft": "We chose a rescue dog in May and bought him a collar.",
        "memory": "We chose a rescue dog in May.",
        "expected_grounded": "We chose a rescue dog",
        "expected_ungrounded": "bought him a collar",
    },
    {
        "draft": "I started a podcast in January and met Sam Altman.",
        "memory": "I started a podcast in January.",
        "expected_grounded": "I started a podcast",
        "expected_ungrounded": "met Sam Altman",
    },
    {
        "draft": "You started running in 2023 and finished a marathon in Berlin.",
        "memory": "You started running in 2023.",
        "expected_grounded": "You started running",
        "expected_ungrounded": "finished a marathon in Berlin",
    },
    {
        "draft": "I created a mobile app in 2024 and started a company around it.",
        "memory": "I created a mobile app in 2024.",
        "expected_grounded": "I created a mobile app",
        "expected_ungrounded": "started a company around it",
    },
    {
        "draft": "I learned Mandarin last year and passed HSK 5.",
        "memory": "I learned Mandarin last year.",
        "expected_grounded": "I learned Mandarin",
        "expected_ungrounded": "passed HSK 5",
    },
    {
        "draft": "We planned the kitchen renovation in April and bought marble counters.",
        "memory": "We planned the kitchen renovation in April.",
        "expected_grounded": "We planned the kitchen renovation",
        "expected_ungrounded": "bought marble counters",
    },
    {
        "draft": "I chose Postgres in June and started the migration in July.",
        "memory": "I chose Postgres in June.",
        "expected_grounded": "I chose Postgres",
        "expected_ungrounded": "started the migration in July",
    },
    {
        "draft": "You decided to use Docker in March and planned the production rollout in May.",
        "memory": "You decided to use Docker in March.",
        "expected_grounded": "You decided to use Docker",
        "expected_ungrounded": "planned the production rollout in May",
    },
    {
        "draft": "I visited the dentist in September and started a new treatment.",
        "memory": "I visited the dentist in September.",
        "expected_grounded": "I visited the dentist",
        "expected_ungrounded": "started a new treatment",
    },
    {
        "draft": "We went skiing in February and visited Zermatt.",
        "memory": "We went skiing in February.",
        "expected_grounded": "We went skiing",
        "expected_ungrounded": "visited Zermatt",
    },
    {
        "draft": "I bought a house in Austin in 2025 and started a garden.",
        "memory": "I bought a house in Austin in 2025.",
        "expected_grounded": "I bought a house in Austin",
        "expected_ungrounded": "started a garden",
    },
    {
        "draft": "I met your friend Priya last week and planned the project together.",
        "memory": "I met your friend Priya last week.",
        "expected_grounded": "I met your friend Priya",
        "expected_ungrounded": "planned the project together",
    },
    {
        "draft": "I started a company in 2022 and met an investor.",
        "memory": "I started a company in 2022.",
        "expected_grounded": "I started a company",
        "expected_ungrounded": "met an investor",
    },
    {
        "draft": "You studied design in 2019 and worked at IDEO.",
        "memory": "You studied design in 2019.",
        "expected_grounded": "You studied design",
        "expected_ungrounded": "worked at IDEO",
    },
    {
        "draft": "I went to Chicago last weekend and visited the Art Institute.",
        "memory": "I went to Chicago last weekend.",
        "expected_grounded": "I went to Chicago",
        "expected_ungrounded": "visited the Art Institute",
    },
    {
        "draft": "We bought paella on Sunday and visited the neighbors.",
        "memory": "We bought paella on Sunday.",
        "expected_grounded": "We bought paella",
        "expected_ungrounded": "visited the neighbors",
    },
    {
        "draft": "I bought a MacBook in January and built a Linux workstation.",
        "memory": "I bought a MacBook in January.",
        "expected_grounded": "I bought a MacBook",
        "expected_ungrounded": "built a Linux workstation",
    },
    {
        "draft": "I traveled to Japan in 2023 and visited Mount Fuji.",
        "memory": "I traveled to Japan in 2023.",
        "expected_grounded": "I traveled to Japan",
        "expected_ungrounded": "visited Mount Fuji",
    },
    {
        "draft": "You planned a wedding in October and chose a venue in Tuscany.",
        "memory": "You planned a wedding in October.",
        "expected_grounded": "You planned a wedding",
        "expected_ungrounded": "chose a venue in Tuscany",
    },
    {
        "draft": "I moved to Amsterdam in 2021 and joined a fintech.",
        "memory": "I moved to Amsterdam in 2021.",
        "expected_grounded": "I moved to Amsterdam",
        "expected_ungrounded": "joined a fintech",
    },
    {
        "draft": "We planned to sell the RV in May and bought a sailboat.",
        "memory": "We planned to sell the RV in May.",
        "expected_grounded": "We planned to sell the RV",
        "expected_ungrounded": "bought a sailboat",
    },
    {
        "draft": "I finished the Ironman in 2024 and finished in the top ten.",
        "memory": "I finished the Ironman in 2024.",
        "expected_grounded": "I finished the Ironman",
        "expected_ungrounded": "finished in the top ten",
    },
    {
        "draft": "I started therapy in January and started running in March.",
        "memory": "I started therapy in January.",
        "expected_grounded": "I started therapy",
        "expected_ungrounded": "started running in March",
    },
    {
        "draft": "You bought a treadmill in April and worked out every day.",
        "memory": "You bought a treadmill in April.",
        "expected_grounded": "You bought a treadmill",
        "expected_ungrounded": "worked out every day",
    },
    {
        "draft": "I started a newsletter in 2025 and wrote every issue myself.",
        "memory": "I started a newsletter in 2025.",
        "expected_grounded": "I started a newsletter",
        "expected_ungrounded": "wrote every issue myself",
    },
    {
        "draft": "We grew tomatoes in March and started a pepper garden.",
        "memory": "We grew tomatoes in March.",
        "expected_grounded": "We grew tomatoes",
        "expected_ungrounded": "started a pepper garden",
    },
    {
        "draft": "I went to Singapore in June and met the regional team.",
        "memory": "I went to Singapore in June.",
        "expected_grounded": "I went to Singapore",
        "expected_ungrounded": "met the regional team",
    },
    {
        "draft": "I built a garage studio in 2023 and bought new tools.",
        "memory": "I built a garage studio in 2023.",
        "expected_grounded": "I built a garage studio",
        "expected_ungrounded": "bought new tools",
    },
    {
        "draft": "You chose React in 2022 and started the dashboard migration in 2023.",
        "memory": "You chose React in 2022.",
        "expected_grounded": "You chose React",
        "expected_ungrounded": "started the dashboard migration in 2023",
    },
    {
        "draft": "I bought a cat in 2020 and built a scratching post.",
        "memory": "I bought a cat in 2020.",
        "expected_grounded": "I bought a cat",
        "expected_ungrounded": "built a scratching post",
    },
    {
        "draft": "We traveled to Kilimanjaro in 2025 and finished at dawn.",
        "memory": "We traveled to Kilimanjaro in 2025.",
        "expected_grounded": "We traveled to Kilimanjaro",
        "expected_ungrounded": "finished at dawn",
    },
    {
        "draft": "I wrote a book in 2024 and met a film producer.",
        "memory": "I wrote a book in 2024.",
        "expected_grounded": "I wrote a book",
        "expected_ungrounded": "met a film producer",
    },
    {
        "draft": "I learned to weld in 2023 and built a bike rack.",
        "memory": "I learned to weld in 2023.",
        "expected_grounded": "I learned to weld",
        "expected_ungrounded": "built a bike rack",
    },
    {
        "draft": "You started a café in March and planned to hire three baristas.",
        "memory": "You started a café in March.",
        "expected_grounded": "You started a café",
        "expected_ungrounded": "planned to hire three baristas",
    },
    {
        "draft": "I planned a sabbatical in 2024 and traveled across Spain.",
        "memory": "I planned a sabbatical in 2024.",
        "expected_grounded": "I planned a sabbatical",
        "expected_ungrounded": "traveled across Spain",
    },
    {
        "draft": "We bought a projector in May and built a home theater.",
        "memory": "We bought a projector in May.",
        "expected_grounded": "We bought a projector",
        "expected_ungrounded": "built a home theater",
    },
]


GROUNDED_DRAFTS: list[dict] = [
    {
        "draft": "I decided to use SQLite for local testing.",
        "memory": "I decided to use SQLite for local testing.",
        "expected_grounded": "I decided to use SQLite",
    },
    {
        "draft": "You decided to move to Kyoto last March.",
        "memory": "You decided to move to Kyoto last March.",
        "expected_grounded": "You decided to move to Kyoto",
    },
    {
        "draft": "I visited Paris in March.",
        "memory": "I visited Paris in March.",
        "expected_grounded": "I visited Paris",
    },
    {
        "draft": "I bought a Tesla in June.",
        "memory": "I bought a Tesla in June.",
        "expected_grounded": "I bought a Tesla",
    },
    {
        "draft": "We planned a trip to Rome.",
        "memory": "We planned a trip to Rome.",
        "expected_grounded": "We planned a trip to Rome",
    },
    {
        "draft": "I finished the Q3 report on Friday.",
        "memory": "I finished the Q3 report on Friday.",
        "expected_grounded": "I finished the Q3 report",
    },
    {
        "draft": "I lived in Berlin for two years.",
        "memory": "I lived in Berlin for two years.",
        "expected_grounded": "I lived in Berlin",
    },
    {
        "draft": "I moved to Boston in 2024.",
        "memory": "I moved to Boston in 2024.",
        "expected_grounded": "I moved to Boston",
    },
    {
        "draft": "You married Maya in December.",
        "memory": "You married Maya in December.",
        "expected_grounded": "You married Maya",
    },
    {
        "draft": "I quit my job in March.",
        "memory": "I quit my job in March.",
        "expected_grounded": "I quit my job",
    },
    {
        "draft": "You visited Lisbon last March.",
        "memory": "You visited Lisbon last March.",
        "expected_grounded": "You visited Lisbon",
    },
    {
        "draft": "I wrote a novel in 2025.",
        "memory": "I wrote a novel in 2025.",
        "expected_grounded": "I wrote a novel",
    },
    {
        "draft": "I went to the gym on Tuesday.",
        "memory": "I went to the gym on Tuesday.",
        "expected_grounded": "I went to the gym",
    },
    {
        "draft": "I started a podcast in January.",
        "memory": "I started a podcast in January.",
        "expected_grounded": "I started a podcast",
    },
    {
        "draft": "You started running in 2023.",
        "memory": "You started running in 2023.",
        "expected_grounded": "You started running",
    },
    {
        "draft": "I created a mobile app in 2024.",
        "memory": "I created a mobile app in 2024.",
        "expected_grounded": "I created a mobile app",
    },
    {
        "draft": "I learned Mandarin last year.",
        "memory": "I learned Mandarin last year.",
        "expected_grounded": "I learned Mandarin",
    },
    {
        "draft": "I chose Postgres in June.",
        "memory": "I chose Postgres in June.",
        "expected_grounded": "I chose Postgres",
    },
    {
        "draft": "I bought a house in Austin in 2025.",
        "memory": "I bought a house in Austin in 2025.",
        "expected_grounded": "I bought a house in Austin",
    },
    {
        "draft": "You started a café in March.",
        "memory": "You started a café in March.",
        "expected_grounded": "You started a café",
    },
]


def _material(memory: str | None, memory_id: str = "corpus-mem") -> list[GroundingMaterial]:
    if not memory:
        return []
    return [
        GroundingMaterial(
            text=memory,
            memory_id=memory_id,
            memory_type="decision",
            confidence=0.9,
        )
    ]


async def evaluate_grounding_corpus(*, use_nli: bool = False) -> dict:
    """Run the corpus and return precision/recall for the report footer."""

    flagged = 0
    ungrounded_kept = 0
    ungrounded_missed = 0
    grounded_falsely_removed = 0
    grounded_total = 0
    nli_info: dict | None = None

    for item in ADVERSARIAL_DRAFTS:
        material = _material(item.get("memory"))
        claims, _ = audit_grounding(item["draft"], material)
        if use_nli and claims:
            from app.filter.nli_critic import run_nli_audit

            claims, nli_info = await run_nli_audit(claims, material)
        ungrounded_claim = next(
            (c for c in claims if c.text.lower().startswith(item["expected_ungrounded"].lower())),
            None,
        )
        grounded_claim = next(
            (c for c in claims if c.text.lower().startswith(item["expected_grounded"].lower())),
            None,
        )
        grounded_total += 1
        if grounded_claim is not None and grounded_claim.action == "remove":
            grounded_falsely_removed += 1
        if ungrounded_claim is None:
            ungrounded_missed += 1
        elif ungrounded_claim.action in ("remove", "soften", "flag"):
            flagged += 1
        else:
            ungrounded_kept += 1

    for item in GROUNDED_DRAFTS:
        material = _material(item["memory"])
        claims, _ = audit_grounding(item["draft"], material)
        if use_nli and claims:
            from app.filter.nli_critic import run_nli_audit

            claims, nli_info = await run_nli_audit(claims, material)
        grounded_total += 1
        grounded_claim = next(
            (c for c in claims if c.text.lower().startswith(item["expected_grounded"].lower())),
            None,
        )
        if grounded_claim is not None and grounded_claim.action == "remove":
            grounded_falsely_removed += 1

    ungrounded_total = len(ADVERSARIAL_DRAFTS)
    grounded_kept = grounded_total - grounded_falsely_removed
    recall = flagged / ungrounded_total
    false_removal_rate = grounded_falsely_removed / grounded_total
    precision = grounded_kept / (grounded_kept + ungrounded_kept)
    return {
        "ungrounded_total": ungrounded_total,
        "ungrounded_flagged_or_removed": flagged,
        "ungrounded_kept": ungrounded_kept,
        "ungrounded_missed": ungrounded_missed,
        "grounded_total": grounded_total,
        "grounded_falsely_removed": grounded_falsely_removed,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "false_removal_rate": round(false_removal_rate, 4),
        "nli": nli_info,
    }
