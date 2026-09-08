"""Response shaping: the dicts every service return path shares.

Module functions rather than a class, because they hold no state -- they just build the
payload. Kept in one place so /chat, /chat/continue and /jobs/status cannot drift into
returning three slightly different shapes for the same thing, which is exactly what a
frontend then has to special-case.
"""
from app.core.constants import DONE


def join(messages) -> str:
    """Collapse a turn's lines into the single `answer` string the FE also reads."""
    return "\n\n".join(msg for msg in messages if msg)


def visible(messages) -> list:
    """Drop blank/whitespace-only lines.

    Applied BEFORE both the response and the transcript write, so the two cannot
    disagree: ConversationRepository.append_turns skips blank content, and without this
    the FE would render an empty bubble that vanished on refresh.
    """
    return [m for m in messages if m and m.strip()]


def chat_payload(user_id, session_id, conversation_id, stage, messages, answer) -> dict:
    """Shape the JSON a chat turn returns (identical across every return path)."""
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "done": stage == DONE,
        "messages": messages,
        "answer": answer,
    }


def job_payload(
    user_id, session_id, conversation_id, stage, *, done, messages, answer=None
) -> dict:
    """Shape the JSON the FE spinner expects (consistent across every return path)."""
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "done": done,
        "messages": messages,
        "answer": answer if answer is not None else join(messages),
    }
