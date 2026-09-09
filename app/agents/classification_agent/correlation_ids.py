####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Mint the opaque identifier that ties a set of log lines together when no conversation id exists. #
#   1. Expose generate_correlation_id() returning a fresh UUID4 string.                            #
#   2. Give teardown / pre-conversation events a log key so they are still traceable.              #
#   3. Keep identifier generation in one place so every component mints ids the same way.          #
#                                                                                                  #
# Source:-                                                                                         #
#   - uuid (stdlib) supplies the UUID4 generator used to mint each correlation id.                 #
#       - uuid.uuid4:- returns a random 128-bit UUID, stringified to a 36-char id.                 #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

import uuid  # Standard library module used to generate universally unique identifiers              # stdlib uuid


# ========================================= Correlation ids ========================================
def generate_correlation_id() -> str:  # Return a new correlation id string                         # id factory
    """Generate a new, unique correlation id for a set of related log events.

    What this function is:
        - The single source of correlation ids for this agent: a thin wrapper over uuid4() that
          produces one opaque, collision-resistant string.

    Why this exists:
        - Log lines emitted before a conversation id is known (component teardown, construction
          failures) still need a key, so they can be grouped and traced like every other event.

    Security and production notes:
        - UUID4 is random (not sequential), so the id reveals nothing about volume, ordering, or
          timing and is safe to surface in logs. It is an identifier only - never a secret or token.

    Args:
        None.

    Returns:
        A random UUID4 string used to correlate log events.

    Example:
        >>> cid = generate_correlation_id()  # doctest: +SKIP
        >>> isinstance(cid, str) and len(cid) == 36  # doctest: +SKIP
        True
    """
    return str(uuid.uuid4())  # Create a random UUID4 and return it as a string                     # mint id
