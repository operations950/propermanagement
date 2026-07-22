from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RawEvent:
    """Normalized shape every adapter converts its source's native format
    into, before it reaches the classifier (see intake/classifier.py).

    event_type: one of 'booking', 'cancellation', 'maintenance', 'shortage',
        'generic', 'quo_thread' — decides which action the classifier takes.
        'quo_thread' carries a FULL conversation transcript in `body` for
        whole-thread LLM classification, rather than a single message.
    source: matches tickets.Ticket.Source / Reservation.Source values.
    external_id: the source's stable id for this event (email message id,
        call id, reservation confirmation code, ...) — used for idempotent
        get_or_create so re-polling never creates duplicates.
    """

    event_type: str
    source: str
    external_id: str
    title: str = ''
    body: str = ''
    property_name: str = ''
    reporter_name: str = ''
    reporter_email: str = ''
    reporter_phone: str = ''
    check_in: str = ''
    check_out: str = ''
    extra: dict = field(default_factory=dict)


class IntakeAdapter(ABC):
    """One implementation per reactive-task source (shared inbox, shared
    phone line, shared calendar, Airbnb, VRBO, ...). Pull-based because none
    of these sources push webhooks we can just listen on; the scheduler
    calls `pull()` on an interval instead (see proptasks/scheduler.py)."""

    @abstractmethod
    def pull(self) -> list[RawEvent]:
        """Return newly observed events since the last pull. Must be safe
        to call repeatedly — the classifier de-dupes via `external_id`, but
        adapters should still avoid re-fetching the entire history every
        time where the upstream API allows a cheaper incremental query."""
        raise NotImplementedError
