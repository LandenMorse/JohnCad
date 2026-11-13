"""Adding printers through the print agent (AddPrinterTicket state changes).

A teacher files a ticket on /printers/. The agent sees it in its poll, claims
it, adds the printer to Bambuddy, waits until Bambuddy is connected to it, and
reports Bambuddy's id for it (or why it failed). `complete` then creates the
Printer. Every change here is a guarded UPDATE or runs under a row lock, so a
repeated or late report from the agent can't apply twice.
"""
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AddPrinterTicket, Printer

Status = AddPrinterTicket.Status

APPLIED = 'applied'
NOOP = 'noop'
CONFLICT = 'conflict'


def reset_stale_claims():
    """Offer a ticket again if the agent that claimed it went quiet. Adding is
    safe to repeat: the agent first looks the serial number up in Bambuddy."""
    cutoff = timezone.now() - timedelta(seconds=settings.AGENT_CLAIM_TIMEOUT)
    return AddPrinterTicket.objects.filter(
        status=Status.ADDING, claimed_at__lt=cutoff,
    ).update(status=Status.PENDING, agent=None, claimed_at=None)


def tickets_for_agent(agent):
    """Tickets the agent should work on: unclaimed ones, and its own
    unfinished ones (so it resumes after a restart). Oldest first."""
    return (
        AddPrinterTicket.objects.filter(status=Status.PENDING)
        | AddPrinterTicket.objects.filter(status=Status.ADDING, agent=agent)
    ).order_by('created_at')


def claim(agent, ticket):
    """Mark the ticket as being added by `agent`. True if it is now (or
    already was) this agent's."""
    claimed = AddPrinterTicket.objects.filter(pk=ticket.pk, status=Status.PENDING).update(
        status=Status.ADDING, agent=agent, claimed_at=timezone.now(), error='',
    )
    if claimed:
        return True
    return AddPrinterTicket.objects.filter(
        pk=ticket.pk, status=Status.ADDING, agent=agent,
    ).exists()


def _close(ticket, status, **fields):
    """Finish a ticket. The access code is never kept on a closed ticket."""
    ticket.status = status
    ticket.access_code = ''
    ticket.completed_at = timezone.now()
    for name, value in fields.items():
        setattr(ticket, name, value)
    ticket.save()


def complete(ticket, agent, bambuddy_printer_id):
    """The agent added the printer to Bambuddy and Bambuddy is connected to
    it: create the Printer. Returns APPLIED, NOOP (already done) or CONFLICT
    (the ticket was cancelled or had failed)."""
    with transaction.atomic():
        ticket = AddPrinterTicket.objects.select_for_update().get(pk=ticket.pk)
        if ticket.status == Status.SUCCEEDED:
            return NOOP
        if not ticket.is_open:
            return CONFLICT

        existing = Printer.objects.filter(bambuddy_printer_id=bambuddy_printer_id).first()
        if existing is not None:
            _close(
                ticket, Status.FAILED, agent=agent, bambuddy_printer_id=bambuddy_printer_id,
                error=f'Bambuddy already has this printer, and it is set up here as "{existing.name}".',
            )
            return APPLIED
        try:
            with transaction.atomic():
                printer = Printer.objects.create(
                    name=ticket.name,
                    bambuddy_printer_id=bambuddy_printer_id,
                    serial_number=ticket.serial_number,
                    printer_model=ticket.printer_model,
                )
        except IntegrityError:
            _close(
                ticket, Status.FAILED, agent=agent, bambuddy_printer_id=bambuddy_printer_id,
                error=(
                    f'The printer was added to Bambuddy (id {bambuddy_printer_id}), but a '
                    f'printer named "{ticket.name}" already exists here. Ask an admin to '
                    'link it in /admin/.'
                ),
            )
            return APPLIED
        # An agent limited to some printers must be able to print on the one
        # it just added.
        if agent.printers.exists():
            agent.printers.add(printer)
        _close(
            ticket, Status.SUCCEEDED, agent=agent, printer=printer,
            bambuddy_printer_id=bambuddy_printer_id, error='',
        )
    return APPLIED


def fail(ticket, agent, error):
    """The agent couldn't add the printer. False if the ticket isn't open."""
    with transaction.atomic():
        ticket = AddPrinterTicket.objects.select_for_update().get(pk=ticket.pk)
        if not ticket.is_open:
            return False
        _close(ticket, Status.FAILED, agent=agent, error=error or 'The print agent gave no reason.')
    return True


def cancel(ticket):
    """A teacher withdraws a ticket no agent has started on. False if it has
    already been picked up or finished."""
    with transaction.atomic():
        ticket = AddPrinterTicket.objects.select_for_update().get(pk=ticket.pk)
        if ticket.status != Status.PENDING:
            return False
        _close(ticket, Status.CANCELLED)
    return True
