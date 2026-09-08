"""The FIRST classification agent: the user's words -> one KB article.

Stage one of two. This agent gathers detail until it can name the knowledge-base article
that matches the problem; the SECOND agent then decides how that article gets resolved.
The split exists because the two jobs need different context -- this one is a conversation
with the user, the other is a decision about the KB.

CONTRACT (was: POST to the first-classification Function App)
    request   conversation_id, message      <- the old {"conversation_id", "message"} body
    response  still gathering:  {"chat_close": false, "kb_id": null, "summary": null,
                                 "agent_message": "<the next question to ask>"}
              done:             {"chat_close": true, "kb_id": "kb100",
                                 "summary": "<what's wrong, for the next agent>",
                                 "agent_message": ""}

THE AGENT OWNS THE FOLLOW-UP LOOP. The orchestrator does not count questions or cap them:
it shows `agent_message`, stays on the classification stage, and calls again with the
user's reply until `chat_close` is true. So adding or removing a question is a change to
this agent, not to the flow.

`chat_close: true` with no kb_id would strand the flow -- the second agent is given the
kb_id and has nothing else to work from -- so return one only when you have one.
"""
from app.agents.base import Agent


class FirstClassificationAgent(Agent):
    """Turns a described problem into a kb_id plus a summary for the second agent."""

    label = "classify_1"

    def classify(self, conversation_id: str, message: str) -> dict:
        """Advance classification by one turn. Returns the agent's response dict.

        conversation_id is what makes this multi-turn: each answer only means something
        against the questions already asked, and that history lives on the Foundry
        conversation rather than being re-sent by us.
        """
        return self._call(conversation_id=conversation_id, message=message)
