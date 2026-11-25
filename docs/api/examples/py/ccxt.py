from collections.abc import Callable
from datetime import UTC, datetime

from atreides import Client, Refusal
from atreides.models import FeeSchedule


def ccxt_parameters(
    client: Client, venue: str, account_id: str, encode: Callable[[str, str], dict[str, str]]
) -> dict[str, str]:
    # The encoder maps the schedule to the installed CCXT adapter's documented parameters.
    result = client.fees.schedule({'venue': venue, 'accountId': account_id})
    if isinstance(result, Refusal):
        raise RuntimeError(f'{result.code}: {result.message}')
    if not isinstance(result.data, list):
        raise RuntimeError('Expected fee schedules')
    schedule = next((row for row in result.data if isinstance(row, FeeSchedule) and row.venue == venue), None)
    if (
        schedule is None
        or schedule.approval is None
        or schedule.approval.state not in {'approved', 'not_needed'}
        or not schedule.builderRef
        or not schedule.field
        or schedule.validUntil.root <= datetime.now(UTC)
    ):
        raise RuntimeError('A current approved fee schedule is required')
    parameters = encode(schedule.field, schedule.builderRef)
    if not parameters:
        raise RuntimeError('The CCXT venue mapping returned no parameters')
    return parameters
