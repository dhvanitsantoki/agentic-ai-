from .models import FollowUpSequence, FollowUpStep

# Owner-specified default schedule: Day 0 intro, then Day 3, 7, 14 and a final Day 30.
DEFAULT_STEPS = [
    (1, 3, "Short, polite check that the introduction arrived; restate the single most relevant reason in one line; ask one easy question."),
    (2, 7, "Add new value: point to the specific verified product line that matches their range and offer the catalogue."),
    (3, 14, "Different angle: OEM/private-label or supply flexibility (only approved capabilities); ask who handles sourcing if not them."),
    (4, 30, "Final, courteous close-the-loop note: say this is the last follow-up and they can reply any time in future."),
]


def seed_sequences() -> int:
    seq, created = FollowUpSequence.objects.get_or_create(
        name="Standard export follow-up",
        defaults={"description": "Day 0 introduction, follow-ups on Day 3, 7, 14 and a final one on Day 30.", "is_default": True},
    )
    if created:
        for number, offset, angle in DEFAULT_STEPS:
            FollowUpStep.objects.create(sequence=seq, step_number=number, day_offset=offset, angle=angle, is_final=number == 4)
    return int(created)
