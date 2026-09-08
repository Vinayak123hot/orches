"""The SECOND classification agent: given a KB article, how do we resolve it?

Stage two of two, and the agent with the most consequence in the app: its answer decides
whether the user is shown manual steps, asked another question, or has a script run on
their machine.

CONTRACT (was: POST to the second-classification Function App)
    request   conversation_id, kb_id, message
              message is "" on the FIRST call and the user's reply on ask_user follow-ups
    response  a ValidationResponse dict:

        success            false -> we could not get to an answer (M2/U5/U6/N2). The flow
                           shows agent_message (or a generic line) and routes to the team.
        validated          false -> a real answer, but not automatable (N1, e.g. the KB
                           has no fix). Same tail: show the message, route to the team.
        action             "manual"    -> steps for the user to follow (M1)
                           "automatic" -> we may run something on the device
        steps              the manual instructions, reference link included
        user_params        truthy when the fix needs input from the user
        follup_flag        true  -> still collecting input; ask follow_up_question (U1/U2/U4)
                           false -> input complete, run it (U3)
        follow_up_question the next question to put to the user
        script_name        what to run, once the inputs are collected (U3)
        script_params      the collected inputs for that script
        agent_message      a line that is SAFE to show the user, in any outcome
        error              the technical cause. Logged and stored, NEVER shown to a user
        stop_reason        why the agent gave up, for the logs

THE AGENT OWNS THE FOLLOW-UP COUNT. We neither send nor track it: the agent asks, and when
it has asked enough it answers success=false itself (U5). Nothing in the flow caps it.

ANY unexpected `action` is routed to the support team rather than run -- see
_route_second_classification in app/domain/flow.py. An automated action must be asked for
explicitly and spelled correctly; drift must never be the thing that starts a script on
someone's machine.

THIS CLASS NEVER RAISES. Every failure becomes {"success": false, "error": <cause>} so the
flow has ONE shape to route on, and a transport failure is handled exactly like the
agent's own "I give up". That policy is the reason validate() wraps _call rather than
being it.
"""
from app.agents.base import Agent
from app.core.config import logger


class SecondClassificationAgent(Agent):
    """Decides how a classified issue gets resolved. Returns; does not raise."""

    label = "classify_2"

    def validate(self, conversation_id: str, kb_id, message: str = "") -> dict:
        """Ask how to resolve this KB article. Returns a ValidationResponse dict.

        Never raises: any failure below becomes a {"success": false} dict, so the flow's
        router handles every failure the same way -- show a user-safe line, stash the raw
        cause in flow_vars["last_error"] for the logs.
        """
        try:
            response = self._call(
                conversation_id=conversation_id, kb_id=kb_id, message=message
            )
        except Exception as exc:
            logger.error(
                "second classification call failed conv_id=%s (%s): %s",
                conversation_id, type(exc).__name__, exc,
            )
            return self.failure(str(exc))

        if not isinstance(response, dict):
            # A non-dict cannot be routed -- every branch in the flow reads named keys.
            logger.error(
                "second classification returned %s, expected a dict conv_id=%s",
                type(response).__name__, conversation_id,
            )
            return self.failure(
                f"expected a ValidationResponse dict, got {type(response).__name__}"
            )
        return response

    @staticmethod
    def failure(error: str) -> dict:
        """A ValidationResponse-shaped failure.

        agent_message is left EMPTY on purpose: the flow then shows its own generic line
        (constants.SECOND_CLASS_FALLBACK), so a raw technical cause can never reach a
        user's screen. The cause goes in `error`, which is logged and stored on the
        conversation row for debugging.
        """
        return {
            "success": False,
            "validated": False,
            "action": None,
            "agent_message": "",
            "error": error,
        }
