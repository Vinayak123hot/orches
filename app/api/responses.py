"""The payloads returned when a turn fails, or when a conversation has already ended.

These are PRESENTATION decisions -- what a user sees when something breaks -- which is why
they live in the api layer and not in services/. The services raise a domain error; this
module decides that a failed turn should look like a normal assistant bubble rather than
a raw HTTP 500.
"""
from app.core.constants import DONE

FALLBACK_MESSAGE = (
    "Sorry, something went wrong on our side and I couldn't process that just now. "
    "Please try again in a moment."
)

# Used when a ServiceNow ticket was ALREADY raised before the turn failed. Telling that
# user to "try again" is what produced duplicate tickets: they would start over and a
# second incident would be opened for the same problem. Handing them the number and
# saying they need not start again removes the reason to retry.
ESCALATED_MESSAGE = (
    "Something went wrong on our side before I could finish. Your ticket {incident} has "
    "already been raised and the support team will follow up -- you can track it in "
    "ServiceNow. You don't need to start again."
)


def fallback_chat_response(
    user_id, session_id=None, conversation_id=None, *, stage=DONE, done: bool = True
) -> dict:
    """Safe chat payload returned when a turn fails unexpectedly.

    The defaults describe a FIRST turn that failed (POST /chat): nothing was saved, so
    there is no conversation to continue and the FE must start fresh.

    /chat/continue passes the real stage with done=False, because there a failure leaves
    the conversation untouched and the user can just resend the same message. Claiming
    DONE there used to throw away a working conversation -- and produce a duplicate
    ServiceNow incident when the user started over.

    Shaped like a normal /chat response so the frontend renders it as an assistant
    bubble (with error=True) instead of choking on a raw HTTP 500. Client errors
    (404/409/422) are still raised normally so the FE can handle them.

    stage is DONE, not a made-up "ERROR". Two reasons:
      * it agrees with done=True. A turn that failed left NOTHING saved -- no
        conversation row, and session_id is None -- so there is genuinely nothing to
        continue. DONE is the frontend's existing signal for "start a new chat", which
        is exactly the right recovery.
      * "ERROR" was not a real stage: absent from core/constants.py, impossible for the
        flow to produce, and unhandled by it. A frontend keying on stage == "DONE" would
        have missed it and shown a dead conversation with no way forward.

    error=True is what distinguishes "failed" from "completed normally", so the FE shows
    the error wording rather than a normal ending. The FE should branch on done/error --
    never on the stage string, which is internal flow state.
    """
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "done": done,
        "error": True,
        "messages": [FALLBACK_MESSAGE],
        "answer": FALLBACK_MESSAGE,
    }


def ended_chat_response(user_id, session_id, message: str) -> dict:
    """Payload for a conversation that has already finished.

    Deliberately NOT an error and NOT a 409. The conversation completed normally --
    there is simply nothing left to continue -- so error=False and the message is the
    domain error's own text, which is author-written and safe to display.

    Why a 200 rather than the 409 this used to be: the frontend needs done=True to reset
    itself and start a new chat. A 409 carries only {"detail": ...} -- no stage, no done
    flag, no messages -- so the FE would need a separate branch to recover, and an
    unhandled one leaves the user at a dead end. One response shape keeps the FE's
    rendering path single.
    """
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": None,
        "stage": DONE,
        "done": True,
        "error": False,
        "messages": [message],
        "answer": message,
    }


def escalated_chat_response(user_id, incident_id) -> dict:
    """Failure payload for when a ticket already exists.

    Same shape as any other chat response, so the FE renders it as an assistant bubble.
    done=True because a failed first turn saved nothing -- there is no conversation to
    continue -- but the message deliberately does NOT ask the user to retry, because the
    work is already in ServiceNow's hands.

    error stays True: something did go wrong, and the FE should style it as such.
    """
    message = ESCALATED_MESSAGE.format(incident=incident_id)
    return {
        "user_id": user_id,
        "session_id": None,
        "conversation_id": None,
        "stage": DONE,
        "done": True,
        "error": True,
        "messages": [message],
        "answer": message,
    }
