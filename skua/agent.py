"""Milestone 3 text turns. No tool catalog or execution is exposed."""
import asyncio

from .client import ChatClient, ModelError
from .session import Session

SYSTEM_MESSAGE = {'role': 'system', 'content': (
    'You are Skua, a terminal coding assistant. This version supports conversation only. '
    'You cannot read project files, execute commands, or edit files. Do not claim to have done so. '
    'Help with text and code supplied by the user.'
)}


async def run_turn(session: Session, client: ChatClient, text: str, *, on_text=lambda _: None,
                   on_notice=lambda _: None) -> None:
    session.begin(text)
    partial = []
    def delta(value):
        partial.append(value)
        on_text(value)
    def retry(attempt, delay):
        on_notice(f'Temporary endpoint failure; retry {attempt}/3 in {delay:g}s.')
    try:
        reply = await client.complete([SYSTEM_MESSAGE, *session.messages()], on_text=delta, on_retry=retry)
        if reply.calls:
            raise ModelError('unsupported_tools', 'Tools are unavailable in this milestone. Nothing was executed.')
        session.finish(reply.text)
    except asyncio.CancelledError:
        session.notice('cancelled', 'Response cancelled; partial text is incomplete.')
        session.finish(''.join(partial), 'cancelled', 'user_cancelled')
        on_notice('Response cancelled. Returned to the prompt.')
    except ModelError as exc:
        session.notice(exc.code, str(exc))
        session.finish(''.join(partial), 'error', exc.code)
        on_notice(str(exc))
