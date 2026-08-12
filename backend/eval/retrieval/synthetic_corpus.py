"""Deterministic synthetic corpus + 50-question retrieval eval set.

The human's real questions live outside the repo; this set exists so CI can
exercise the full harness offline. It is a quality probe, not the acceptance
gate (the acceptance gate is the personal 50-question set).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class SyntheticMemory:
    key: str
    text: str
    memory_type: str = "observation"
    importance: float = 0.5
    privacy_level: str = "normal"
    event_time: datetime | None = None


@dataclass
class Question:
    query: str
    expected_keys: list[str]


@dataclass
class SyntheticCorpus:
    memories: list[SyntheticMemory]
    questions: list[Question]


# 50 hand-authored query -> target-memory pairs across life domains.
_QA: list[tuple[str, str]] = [
    ("Which coding model did I decide to use?", "Decided to use DeepSeek V4 Flash for coding models."),
    ("What database did I pick for local testing?", "Decided to use SQLite for local testing, and document the choice."),
    ("Which embedding model is best for local retrieval?", "Research concluded granite-embedding-97m-multilingual-r2 is strong for local retrieval."),
    ("Do I prefer local-first or cloud storage?", "Prefer local-first storage over cloud-only solutions."),
    ("What is my current side project called?", "Building EV as a persistent personal AI companion."),
    ("Who did I meet for coffee recently?", "Met my friend Maya for coffee."),
    ("What is my best friend's name?", "My best friend is named Arjun."),
    ("Who helped me carry furniture during the move?", "Rohan helped me carry furniture during the apartment move."),
    ("What does my sister do for work?", "My sister Priya is a pediatric nurse."),
    ("Who is my gym partner on Tuesdays?", "Neha is my gym partner on Tuesday mornings."),
    ("What is my sleep goal this month?", "Goal: sleep at least 7 hours every night this month."),
    ("How much water do I want to drink each day?", "Goal: drink 2.5 liters of water each day."),
    ("Which food makes me bloated?", "Avoiding gluten because it makes me bloated."),
    ("When do I take my thyroid medication?", "Take levothyroxine at 8am with water."),
    ("Which knee hurts after long runs?", "Right knee aches after long runs."),
    ("What is my target for the analytics dashboard?", "Goal: ship the analytics dashboard by end of quarter."),
    ("Who is my manager at work?", "My manager is Meera at work."),
    ("What did I decide about the startup offer?", "Decided to decline the startup offer and stay at current job."),
    ("Where am I presenting in October?", "Presenting at PyCon India in October."),
    ("When is the migration project due?", "The migration project is due on November 15."),
    ("Do I prefer coffee or tea in the morning?", "Prefer tea over coffee in the morning."),
    ("What music genre do I love?", "Love indie folk music playlists."),
    ("Which cuisine do I prefer?", "Prefer Japanese food over Italian."),
    ("When is my best focus time?", "Feel like mornings are the best time to focus."),
    ("What kind of movies do I enjoy?", "Enjoy sci-fi movies with strong character arcs."),
    ("Where do I want to travel next spring?", "Goal: visit Kyoto in spring 2027."),
    ("What was my favorite trip?", "The hiking trip to Manali was my favorite."),
    ("Which country did I visit last summer?", "Traveled to Portugal last summer."),
    ("Where am I flying for Ritu's wedding?", "Flying to Jaipur for Ritu's wedding in December."),
    ("Do I prefer window or aisle seats?", "Prefer window seats on long flights."),
    ("What time do I usually wake up?", "Usually wake up at 6:30am."),
    ("How often do I work out?", "Work out three times a week."),
    ("When do I write in my journal?", "Write in my journal every evening."),
    ("What do I do on Sunday mornings?", "Sunday mornings are for long breakfasts and no plans."),
    ("What is my bedtime reading routine?", "Read 20 pages before bed."),
    ("What laptop do I use for daily work?", "Use a MacBook Air M2 for daily work."),
    ("Which code editor do I prefer?", "Prefer VS Code over other editors."),
    ("What phone do I have?", "My phone is a Pixel 9."),
    ("Where do my backups go?", "Backups go to an encrypted local drive."),
    ("What keyboard do I love?", "Love the mechanical keyboard with brown switches."),
    ("What is my running goal?", "Goal: run a half marathon in March."),
    ("Which language am I learning?", "Learning Japanese on Duolingo."),
    ("What is my novel called?", "Working on a sci-fi novel called Orbital Garden."),
    ("What do I want to automate?", "Goal: automate my morning report generation."),
    ("Which course am I taking this fall?", "Taking a systems design course this fall."),
    ("What did I decide about the apartment lease?", "Decided to renew the apartment lease for another year."),
    ("How much of my salary do I save?", "Saving 20% of my salary each month."),
    ("What did I buy for long-term savings?", "Bought index funds for long-term savings."),
    ("Did I get the car serviced before the trip?", "Decided to service the car before the long trip."),
    ("What is my budget for the December trip?", "Budget for the December trip is 80,000 rupees."),
]

def _memory_type_for(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith(("goal:", "decided", "budget", "saving", "bought", "taking", "learning")):
        return "decision" if lowered.startswith(("decided", "bought", "taking")) else "goal"
    if lowered.startswith(("prefer", "love", "enjoy", "feel like")):
        return "preference"
    return "fact"


def build_synthetic_corpus(
    *,
    seed: int = 42,
    distractor_count: int = 150,
) -> SyntheticCorpus:
    """Deterministic corpus: 50 targets + distractors + 50 questions.

    The default total (200 memories) matches the production candidate cap
    (``max_retrieval_memories * 4``), so no target can be silently excluded
    before scoring. Timestamps are spread over the past year relative to
    "now" so recency is realistic for both providers.
    """

    rng = random.Random(seed)
    memories: list[SyntheticMemory] = []
    questions: list[Question] = []
    now = datetime.now(UTC)

    for index, (query, text) in enumerate(_QA):
        key = f"t{index + 1:02d}"
        memories.append(
            SyntheticMemory(
                key=key,
                text=text,
                memory_type=_memory_type_for(text),
                importance=round(0.6 + 0.3 * (index % 3) / 2, 2),
                event_time=now - timedelta(days=(index % 280) + 20),
            )
        )
        questions.append(Question(query=query, expected_keys=[key]))

    people = ["Aditi", "Vikram", "Sana", "Kabir", "Ishaan", "Tara", "Dev", "Noor"]
    places = ["cafe", "park", "library", "gym", "airport", "cinema", "bazaar", "river"]
    movies = ["Interstellar", "Dune", "Her", "Arrival", "Past Lives", "Gravity"]
    restaurants = ["Sushi Koi", "The Green Bowl", "Casa Mia", "Nihari House", "Ramen Lab"]
    for i in range(distractor_count):
        kind = rng.randrange(4)
        if kind == 0:
            text = f"Met {rng.choice(people)} at the {rng.choice(places)}."
        elif kind == 1:
            text = f"Watched {rng.choice(movies)} last {rng.choice(['week', 'month'])}."
        elif kind == 2:
            text = f"Ate at {rng.choice(restaurants)} and liked the {rng.choice(['ramen', 'salad', 'curry', 'pizza'])}."
        else:
            text = (
                f"Read about {rng.choice(['quantum computing', 'urban farming', 'jazz history', 'trail running', 'ceramics'])} "
                f"on {rng.choice(['a flight', 'the train', 'Sunday'])}."
            )
        memories.append(
            SyntheticMemory(
                key=f"d{i + 1:03d}",
                text=text,
                memory_type="observation",
                importance=round(rng.uniform(0.3, 0.7), 2),
                event_time=now - timedelta(days=((i * 7) % 300) + 5),
            )
        )
    return SyntheticCorpus(memories=memories, questions=questions)
