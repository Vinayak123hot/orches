"""What a device job's report MEANS -- pure functions, no I/O.

A device agent hands back whatever its runner produced. This module turns that into the
only three fields the rest of the app is allowed to know about:

    state    ->  running | done | failed. For CODE to branch on.
    message  ->  one line safe to SHOW THE USER, whatever the state. Our own wording, or
                 the agent's `userMessage` when it provides one.
    output   ->  raw stdout/stderr from the script on the user's device. Stored in
                 conversations.job_output for support, and NEVER returned to the browser.

`message` vs `output` is a SECURITY boundary, not a naming preference.

The scripts belong to other teams and can change without our code changing, so their
output cannot be treated as display text. Real remediation output routinely contains
profile paths (C:\\Users\\jdoe\\...\\jdoe@company.com.ost), registry values, mailbox
addresses and internal server names -- and anything we show is also written into the
transcript, so it would be replayed on every history load. Deciding it in ONE pure
function beats trusting two agent implementations to each decide it the same way.

WHY THIS IS NOT IN THE AGENTS. An agent knows how its run works; it does not know what
this product shows a user, what goes into the transcript, or how a stale device report is
told apart from a fresh one. Those are the caller's concerns, and they are identical for
both device agents.

WHY IT IS ITS OWN MODULE, and not part of JobRunner: nothing here touches an agent, a
clock we don't control, or a database, so every branch below can be tested by passing a
dict -- which matters, because these branches are the difference between telling a user
their machine was fixed and telling them it wasn't.
"""
from datetime import datetime, timedelta, timezone

from app.core.config import logger

# Safety margin so tiny clock differences between our host and the device management
# service cannot make us reject our own fresh result as "stale". 2 min is well under a
# 3-15 min run.
BASELINE_SKEW = timedelta(seconds=120)

# Detection is still in flight for these -> the run isn't done reporting yet.
TRANSIENT_DETECTION = (None, "", "unknown", "pending")

# Map any terminal word an agent might send to our 3-word contract; unknown -> running.
STATE_MAP = {
    "done": "done", "success": "done", "completed": "done", "skipped": "done",
    "failed": "failed", "error": "failed", "timed_out": "failed",
    "not_applicable": "failed",
}


def baseline_stamp() -> str:
    """UTC timestamp captured at trigger; a device report NEWER than this is OUR run.

    Stored on the conversation row (job_baseline) and passed back to derive() on every
    poll. Without it, a run-state row left on the device by a PREVIOUS run of the same
    script reads as a finished result the instant we start -- so the user would be told
    their new problem was fixed by an old repair.
    """
    return (datetime.now(timezone.utc) - BASELINE_SKEW).isoformat()


def derive(data: dict, baseline: str = "") -> dict:
    """Collapse one status report into {state, message, output}.

    Accepts either shape an agent may return:
      * RAW device run-state fields (detectionState, remediationState,
        lastStateUpdateDateTime, the script outputs) -- interpreted below;
      * an already-normalised {"state": ..., "userMessage": ..., "result": ...}.
    """
    if "detectionState" in data or "remediationState" in data:
        return _from_device_run_state(data, baseline)
    return _from_normalised(data)


def _from_normalised(data: dict) -> dict:
    """An agent that already reports a state word.

    Its `result` is treated as RAW OUTPUT, not as a message: we don't know how it was
    written, so it is safe-by-default and only surfaces to the user if the agent also
    sends a field written for them (`userMessage`).
    """
    raw = (data.get("state") or data.get("status") or "running").lower()
    return {
        "state": STATE_MAP.get(raw, "running"),
        "message": (
            data.get("userMessage") or data.get("progress")
            or data.get("message") or "Working..."
        ),
        "output": data.get("result"),
    }


def _from_device_run_state(data: dict, baseline: str) -> dict:
    """Interpret RAW device run-state fields.

    Assumes the fields are at the TOP LEVEL of the dict. If an agent nests them (e.g.
    under data["value"][0]), unwrap in the agent before returning.

    Two steps: (1) the gate -- is a FRESH result from our trigger back yet? -- and only
    then (2) the outcome -- did the detection/remediation scripts succeed?
    """
    detection = data.get("detectionState")
    remediation = data.get("remediationState")
    last_updated = data.get("lastStateUpdateDateTime")
    # Everything the device printed, kept together. Goes to conversations.job_output for
    # support; never to the browser.
    raw_output = (
        data.get("postRemediationDetectionScriptOutput")
        or data.get("preRemediationDetectionScriptOutput")
        or data.get("remediationScriptError")
        or data.get("preRemediationDetectionScriptError")
    )
    # If an agent provides a field written FOR the end user, prefer it over our generic
    # wording (see the note in the module docstring).
    user_message = data.get("userMessage")

    # (1) Gate: no fresh row yet (the job was already triggered, so we can wait), or
    # detection still running -> keep polling.
    if not is_newer(last_updated, baseline) or detection in TRANSIENT_DETECTION:
        return {
            "state": "running",
            "message": f"Working... ({detection or 'queued'})",
            "output": None,
        }

    # (2) Outcome -- the row is fresh and detection is terminal
    # (detection in {success, fail, scriptError, notApplicable}).

    # Hard failures: a script crashed, or remediation ran but didn't resolve it. The
    # script's own error text is the most likely place to carry internal paths and server
    # names, so it goes to `output`, never into the message.
    if detection == "scriptError" or remediation in (
        "remediationFailed", "scriptError"
    ):
        return {
            "state": "failed", "message": "Script error on the device",
            "output": raw_output,
        }
    if detection == "notApplicable":
        return {
            "state": "failed",
            "message": "This fix doesn't apply to your device",
            "output": raw_output,
        }

    # Success #1: remediation actually fixed it (takes priority -- true even if the
    # pre-remediation detectionState still reads 'fail').
    if remediation == "success":
        return {
            "state": "done",
            "message": user_message or "The issue was fixed on your device.",
            "output": raw_output,
        }

    # Success #2: detection found nothing wrong (healthy). Covers a 'skipped' remediation
    # state and detect-only policies where remediation is 'unknown'.
    if detection == "success":
        return {
            "state": "done",
            "message": user_message or "No issues were found on your device.",
            "output": raw_output,
        }

    # Everything left = an issue was FOUND but NOT fixed (detect-only, skipped, or an
    # unrecognised state). Never say "Complete" -- surface it.
    logger.warning(
        "Unresolved/unknown remediation states: detection=%s remediation=%s",
        detection, remediation,
    )
    return {
        "state": "failed",
        "message": "We found an issue but couldn't fix it automatically.",
        "output": raw_output,
    }


def is_newer(last_updated: str, baseline: str) -> bool:
    """True if the run-state row is newer than the baseline (i.e. it's OUR result).

    No baseline -> everything counts (nothing to compare against). A baseline but no
    timestamp on the row -> NOT ours: we cannot show a user a result we can't date.
    """
    if not baseline:
        return True
    if not last_updated:
        return False
    try:
        return _parse(last_updated) > _parse(baseline)
    except ValueError:
        return last_updated > baseline  # ISO-8601 UTC sorts lexically anyway


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
