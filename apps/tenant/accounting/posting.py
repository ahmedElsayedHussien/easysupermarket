"""
The one way to book a journal entry from code:

    post_entry(date=…, reference=…, description=…, lines=[(account, debit, credit, description), …],
               user=…, source=<document>)

Creates the DRAFT header, adds every non-zero line in one INSERT (bulk_create), and posts it through
JournalEntry.post() — the single gate that checks balance > 0, leaf & active accounts, the open
period, and records who / when. A reference already used gets a suffix (the document's number stays
readable). ``source`` links the entry to its document (generic FK).
"""
import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction

ZERO = Decimal('0')


@transaction.atomic
def post_entry(*, date=None, reference, description, lines, user=None, source=None):
    from .models import JournalEntry, JournalItem
    from apps.tenant.services.journal_service import unique_reference
    rows = []
    for n, line in enumerate(lines, start=1):
        account, debit, credit = line[0], Decimal(line[1] or 0), Decimal(line[2] or 0)
        desc = (line[3] if len(line) > 3 and line[3] else description) or ''
        if account is None:
            raise ValidationError(f'السطر {n} في القيد "{description}" مالوش حساب.')
        if not debit and not credit:
            continue                                     # zero lines are dropped
        rows.append((account, debit, credit, desc[:500]))
    entry = JournalEntry.objects.create(
        date=date or datetime.date.today(), reference=unique_reference(reference), description=description,
        status=JournalEntry.DRAFT, created_by=user if getattr(user, 'pk', None) else None,
        content_type=ContentType.objects.get_for_model(source) if source is not None else None,
        object_id=source.pk if source is not None else None,
    )
    JournalItem.objects.bulk_create([JournalItem(entry=entry, account=a, debit=d, credit=c, description=t)
                                     for a, d, c, t in rows])
    entry.post(user)
    return entry
