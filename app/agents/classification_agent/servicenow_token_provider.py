####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Supply the bearer token the knowledge search endpoint is called with.                            #
#   1. Obtain an access token from the identity provider using the client-credentials grant.       #
#   2. Hold it until shortly before it expires, then obtain a fresh one.                           #
#   3. Allow the held token to be discarded, so a rejected call can retry with a new one.          #
#   4. Offer the same interface for a token supplied directly, for a fixed-token deployment.       #
#                                                                                                  #
# Why the token is held rather than fetched per call:                                              #
#   A token is valid for about an hour, and one chat turn can run several searches. Fetching one    #
#   per search would add a round trip to the identity provider before every search, and would put  #
#   a request on that endpoint for every message any user sends. Holding one and replacing it just #
#   before it expires means every call carries a valid token while the identity provider is        #
#   contacted roughly once an hour per worker.                                                     #
#                                                                                                  #
# Source:-                                                                                         #
#   - requests performs the token request over a pooled session.                                   #
#   - threading guards the held token so worker threads cannot each fetch their own.               #
#   - backoff_retry supplies run_with_retry for transient failures at the identity provider.       #
#   - app.event_hub supplies LogFactory / StructuredLogger for the token events.                   #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

import threading  # Guards the held token across worker threads                                     # stdlib threading
import time  # Monotonic clock deciding when the held token is due for replacement                  # stdlib time
from typing import Any, Optional  # Type hints for the response object and optional values          # stdlib typing

import requests  # Performs the token request                                                       # http client
from requests.adapters import HTTPAdapter  # Lets the connection pool size be set explicitly        # pool adapter

from .backoff_retry import run_with_retry  # Exponential-backoff runner for transient failures       # backoff retry
from app.event_hub import LogFactory, StructuredLogger  # Logger factory + structured logger        # telemetry

# Seconds of headroom kept in front of the stated expiry. A token is replaced this long before it
# actually expires, so one cannot lapse between being handed out and the request reaching the
# service, and small clock differences between this host and the identity provider do not matter.
_EXPIRY_SAFETY_MARGIN_SECONDS = 120  # Replace the held token this long before it expires            # safety margin

# Used when the identity provider's reply carries no usable lifetime.
_FALLBACK_LIFETIME_SECONDS = 300  # Hold a token this long when no lifetime was stated               # fallback life

# Status codes worth trying again when asking for a token.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})  # Transient statuses  # retry statuses


# =========================================== Exceptions ==========================================
class TokenRequestError(Exception):
    """Raised when an access token could not be obtained.

    What this class is:
        - The single error this module raises, so the search client reasons about one type however
          the token request failed.

    Why this exists:
        - To keep the message generic. A failure here names no URL, client id or secret, so nothing
          identifying the application's credentials can travel back to a caller.

    Example:
        >>> raise TokenRequestError("An access token could not be obtained.")  # doctest: +SKIP
    """


class _RetryableTokenResponse(Exception):
    """Internal signal that a token response carried a status worth trying again."""


# ======================================= Static token provider ===================================
class StaticTokenProvider:
    """Hand back a token that was supplied directly.

    What this class is:
        - The provider used where a token is configured rather than requested: it returns the same
          value every time and has nothing to refresh.

    Why this exists:
        - So the search client depends on one interface. Whether a token is configured or fetched
          is settled once, where the provider is built, and never at the call site.

    Example:
        >>> StaticTokenProvider("abc").get_token("cid")  # doctest: +SKIP
        'abc'
    """

    def __init__(self, token: str) -> None:
        """Store the configured token.

        Args:
            token: The bearer token to present on every request.

        Returns:
            None.

        Example:
            >>> StaticTokenProvider("abc")  # doctest: +SKIP
        """
        self._token = token  # The configured token                                                  # token

    def get_token(self, correlation_id: str) -> str:
        """Return the configured token.

        Args:
            correlation_id: The end-to-end correlation id, accepted for a common interface.

        Returns:
            The configured bearer token.

        Example:
            >>> StaticTokenProvider("abc").get_token("cid")  # doctest: +SKIP
            'abc'
        """
        return self._token  # Always the same value                                                  # return token

    def invalidate(self) -> None:
        """Do nothing: a configured token has no replacement to fetch.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> StaticTokenProvider("abc").invalidate()  # doctest: +SKIP
        """
        return None  # There is nothing to discard                                                   # no-op

    def close(self) -> None:
        """Release nothing. Present so every provider closes the same way.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> StaticTokenProvider("abc").close()  # doctest: +SKIP
        """
        return None  # Nothing is held open                                                          # no-op


# =================================== Client-credentials provider =================================
class ClientCredentialsTokenProvider:
    """Obtain and hold an access token using the client-credentials grant.

    What this class is:
        - The provider that asks the identity provider for a token, holds it while it is valid, and
          replaces it shortly before it expires.

    Why this exists:
        - So a token never has to be produced by hand or pasted into configuration. The application
          presents its own credentials and receives a token whose lifetime it manages itself.

    Security and production notes:
        1. The client secret and the access token are held in memory only. Neither is ever logged,
           and the log records a token's remaining lifetime rather than any part of its value.
        2. Construction takes no network: the first token is requested when one is first needed.
        3. A lock guards the held token, so a burst of concurrent turns on a cold worker results in
           one token request rather than one per thread.

    Example:
        >>> provider.get_token("cid")  # doctest: +SKIP
        'eyJ0eXAiOi...'
    """

    def __init__(  # Configure the identity provider, the credentials, and the call policy
        self,
        token_url: str,  # Endpoint the token is requested from                                      # token url
        client_id: str,  # The application's own identifier                                          # client id
        client_secret: str,  # The application's own secret                                          # client secret
        scope: str,  # The scope the token is requested for                                          # scope
        request_timeout_seconds: int,  # Per-call timeout for the token request                      # timeout
        verify_tls: bool,  # Whether the server certificate is verified                              # verify tls
        log_factory: LogFactory,  # Factory used to obtain a structured logger                       # log factory
        retry_max_attempts: int,  # Maximum attempts per token request                               # attempts
        retry_base_delay_seconds: float,  # Base delay for exponential backoff                       # base delay
        retry_max_delay_seconds: float,  # Upper bound on a single backoff wait                      # max delay
    ) -> None:
        """Store the credentials and the call policy, and prepare the session. No network work.

        What this method is:
            - The constructor. It keeps what a token request needs and builds one reusable session.
              No token is requested here.

        Why construction is free of network work:
            - So a worker can build the provider while starting up and only contact the identity
              provider when a search actually happens.

        Security and production notes:
            1. The secret is held on the instance and sent only in the token request body. It is
               never placed in a URL, where it could reach a proxy or access log.

        Args:
            token_url: Endpoint the token is requested from.
            client_id: The application's own identifier.
            client_secret: The application's own secret.
            scope: The scope the token is requested for.
            request_timeout_seconds: Per-call timeout for the token request.
            verify_tls: Whether the server certificate is verified.
            log_factory: Factory used to obtain a structured logger.
            retry_max_attempts: Maximum attempts per token request.
            retry_base_delay_seconds: Base backoff delay.
            retry_max_delay_seconds: Upper bound on a single backoff wait.

        Returns:
            None.

        Example:
            >>> ClientCredentialsTokenProvider(  # doctest: +SKIP
            ...     "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token",
            ...     "<client id>", "<secret>", "api://<app>/.default", 20, True, log_factory, 3, 0.5, 8.0)
        """
        self._token_url = token_url  # Endpoint the token is requested from                          # token url
        self._client_id = client_id  # The application's own identifier                              # client id
        self._client_secret = client_secret  # The application's own secret                          # client secret
        self._scope = scope  # The scope the token is requested for                                  # scope
        self._request_timeout_seconds = request_timeout_seconds  # Per-call timeout                  # timeout
        self._verify_tls = verify_tls  # Certificate verification switch                             # verify tls
        self._logger: StructuredLogger = log_factory.get_logger("servicenow_token_provider")  # Named logger  # logger
        self._retry_max_attempts = retry_max_attempts  # Attempt cap                                 # attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds  # Base backoff delay              # base delay
        self._retry_max_delay_seconds = retry_max_delay_seconds  # Backoff ceiling                   # max delay

        self._held_token: Optional[str] = None  # The token currently in hand                        # held token
        self._replace_at: float = 0.0  # Monotonic instant the held token is due for replacement     # replace at
        self._lock = threading.Lock()  # Guards the two fields above across worker threads           # lock

        self._session = requests.Session()  # One pooled session for token requests                  # build session
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4)  # A small pool is plenty           # build adapter
        self._session.mount("https://", adapter)  # Reuse connections to the identity provider       # mount https

    # ============================================ Public API =====================================
    def get_token(self, correlation_id: str) -> str:
        """Return a valid access token, obtaining a fresh one when the held one is due.

        What this method does:
            - Returns the held token while it has time left, and otherwise obtains a new one before
              returning it.

        Why the check happens under a lock:
            - Several worker threads can reach this at the same moment on a cold worker or just
              after a token falls due. Without the lock each would request its own.

        Security and production notes:
            1. The returned value is a credential. It is placed in a request header by the caller
               and must never be logged or stored.

        Args:
            correlation_id: The end-to-end correlation id for this turn.

        Returns:
            A valid access token.

        Raises:
            TokenRequestError: If a token could not be obtained.

        Example:
            >>> provider.get_token("cid")  # doctest: +SKIP
            'eyJ0eXAiOi...'
        """
        with self._lock:  # One thread decides, and any others wait for its result                   # take lock
            if self._held_token and time.monotonic() < self._replace_at:  # Still has time left      # still valid?
                return self._held_token  # Hand back the token already in hand                       # return held
            token, lifetime_seconds = self._request_token(correlation_id)  # Obtain a fresh one      # fetch
            self._held_token = token  # Hold it for the calls that follow                            # hold token
            self._replace_at = time.monotonic() + lifetime_seconds  # When it falls due              # set due time
            return token  # Hand back the fresh token                                                # return fresh

    def invalidate(self) -> None:
        """Discard the held token so the next call obtains a fresh one.

        What this method is:
            - The way a caller reports that the held token was refused, so it is not presented again.

        Why this exists:
            - A token can stop being accepted before its stated expiry, through revocation or a
              clock difference. Discarding it lets one retry recover instead of every call failing
              until the token would have fallen due on its own.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> provider.invalidate()  # doctest: +SKIP
        """
        with self._lock:  # The held token is guarded                                                # take lock
            self._held_token = None  # Drop it                                                       # drop token
            self._replace_at = 0.0  # And its due time                                               # reset due

    def close(self) -> None:
        """Close the pooled session. Best-effort, and safe to call more than once.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> provider.close()  # doctest: +SKIP
        """
        try:  # Release the pooled connections                                                       # try close
            self._session.close()  # Close the session                                               # close
        except Exception:  # Teardown must never raise                                               # guard
            pass  # Nothing useful to do if closing fails                                            # ignore

    # ========================================= Internal helpers ==================================
    def _request_token(self, correlation_id: str) -> "tuple[str, float]":
        """Ask the identity provider for a token and return it with the time it may be held.

        What this method does:
            - Posts the client-credentials grant, reads the access token and the stated lifetime,
              and works out how long the token may be held before it must be replaced.

        Security and production notes:
            1. The credentials go in the request body, never the URL.
            2. Nothing from the reply is logged except the lifetime. A failure logs the status code
               and, where the provider supplied one, its error code - never the description, which
               can echo request values back.

        Args:
            correlation_id: The end-to-end correlation id.

        Returns:
            A tuple of the access token and the seconds it may be held.

        Raises:
            TokenRequestError: If the token could not be obtained or the reply was unusable.

        Example:
            >>> provider._request_token("cid")  # doctest: +SKIP
            ('eyJ0eXAiOi...', 3479.0)
        """
        form_body = {  # The client-credentials grant                                                # build body
            "grant_type": "client_credentials",  # The grant being requested                         # grant type
            "client_id": self._client_id,  # The application's own identifier                        # client id
            "client_secret": self._client_secret,  # The application's own secret                    # client secret
            "scope": self._scope,  # The scope the token is requested for                            # scope
        }

        def _send_request() -> "requests.Response":  # One attempt, retried by the runner below      # one attempt
            """Post the grant and raise when the status is worth trying again."""
            response = self._session.post(  # Request the token                                      # post
                self._token_url,  # Identity provider's token endpoint                               # url
                data=form_body,  # Form-encoded body carrying the credentials                        # body
                timeout=self._request_timeout_seconds,  # Bound this attempt                         # timeout
                verify=self._verify_tls,  # Certificate verification                                 # verify
            )
            if response.status_code in _RETRYABLE_STATUS_CODES:  # Rate-limited or briefly unhealthy  # retryable?
                raise _RetryableTokenResponse(f"status {response.status_code}")  # Signal a retry     # signal retry
            return response  # Hand the response back for reading                                    # return response

        try:  # Run the request through the retry runner and normalise any failure                   # guarded run
            response = run_with_retry(  # Retry on transport failures and retryable statuses          # retry call
                _send_request,  # The attempt to execute                                             # operation
                correlation_id=correlation_id,  # Correlation id for retry logs                      # correlation
                operation_name="servicenow_token",  # Operation label for logs                       # op name
                logger=self._logger,  # Structured logger for retry events                           # logger
                max_attempts=self._retry_max_attempts,  # Attempt cap from config                    # attempts
                base_delay_seconds=self._retry_base_delay_seconds,  # Base backoff delay             # base delay
                max_delay_seconds=self._retry_max_delay_seconds,  # Backoff ceiling                  # max delay
                retryable_exceptions=(  # What triggers another attempt                              # retryable set
                    requests.ConnectionError,  # Connection refused, DNS failure, reset              # conn error
                    requests.Timeout,  # Connect or read timeout                                     # timeout
                    _RetryableTokenResponse,  # A 429 or 5xx status                                  # bad status
                ),
            )
        except (requests.RequestException, _RetryableTokenResponse) as request_error:  # Out of attempts  # failed
            self._logger.log(  # Log the failure without the URL or the credentials                  # log failure
                event="token_request_failed",  # Event name                                          # event
                correlation_id=correlation_id,  # Log key                                            # log key
                level="ERROR",  # Severity level                                                     # level
                error_type=type(request_error).__name__,  # Exception class name                     # error type
            )
            raise TokenRequestError("An access token could not be obtained.") from request_error  # Domain error  # raise

        if response.status_code >= 400:  # The provider refused the credentials or the request        # refused?
            self._logger.log(  # Log the status and the provider's error code, if any                # log refusal
                event="token_request_rejected",  # Event name                                        # event
                correlation_id=correlation_id,  # Log key                                            # log key
                level="ERROR",  # Severity level                                                     # level
                status_code=response.status_code,  # The status the provider returned                # status
                provider_error=self._error_code_of(response),  # Its error code, where it gave one   # error code
            )
            raise TokenRequestError("The access token request was rejected.")  # Domain error         # raise

        try:  # Read the token out of the reply                                                      # decode
            payload = response.json()  # Parse the reply                                             # parse
        except ValueError as decode_error:  # The reply was not JSON                                 # bad json
            raise TokenRequestError("The access token reply could not be read.") from decode_error  # Domain error  # raise

        access_token = payload.get("access_token") if isinstance(payload, dict) else None  # The token  # read token
        if not access_token or not isinstance(access_token, str):  # No usable token in the reply     # no token?
            self._logger.log(  # Log the shape, never the reply itself                               # log shape
                event="token_response_unusable",  # Event name                                       # event
                correlation_id=correlation_id,  # Log key                                            # log key
                level="ERROR",  # Severity level                                                     # level
                keys=sorted(payload.keys()) if isinstance(payload, dict) else [],  # Key names only   # keys
            )
            raise TokenRequestError("The access token reply carried no token.")  # Domain error       # raise

        lifetime_seconds = self._hold_seconds_from(payload)  # How long this token may be held        # hold time
        self._logger.log(  # Record that a token was obtained, by lifetime only                      # log success
            event="token_obtained",  # Event name                                                    # event
            correlation_id=correlation_id,  # Log key                                                # log key
            expires_in_seconds=payload.get("expires_in"),  # The lifetime the provider stated        # stated life
            hold_seconds=round(lifetime_seconds),  # How long it will be held before replacement     # hold time
        )
        return access_token, lifetime_seconds  # The token and how long it may be held               # return pair

    @staticmethod  # Declare a static helper (needs neither instance nor class state)
    def _hold_seconds_from(payload: dict) -> float:
        """Return how long a token may be held, from the lifetime the provider stated.

        What this method does:
            - Subtracts the safety margin from the stated lifetime, and never returns a value at or
              below zero, so a very short lifetime still produces a usable hold.

        Args:
            payload: The decoded token reply.

        Returns:
            The seconds the token may be held before it must be replaced.

        Example:
            >>> ClientCredentialsTokenProvider._hold_seconds_from({"expires_in": 3599})
            3479.0
        """
        try:  # The lifetime arrives as a number or as a string of digits                            # parse life
            stated_lifetime = float(payload.get("expires_in"))  # Seconds the provider stated        # stated life
        except (TypeError, ValueError):  # Absent or not a number                                    # unusable?
            stated_lifetime = float(_FALLBACK_LIFETIME_SECONDS)  # Hold it briefly and ask again      # fallback
        usable_lifetime = stated_lifetime - _EXPIRY_SAFETY_MARGIN_SECONDS  # Keep headroom in front   # apply margin
        if usable_lifetime <= 0:  # The stated lifetime was shorter than the margin                  # too short?
            return max(stated_lifetime / 2.0, 1.0)  # Hold half of it, and always at least a second   # half life
        return usable_lifetime  # Hold it until the margin begins                                    # return hold

    @staticmethod  # Declare a static helper (needs neither instance nor class state)
    def _error_code_of(response: Any) -> Optional[str]:
        """Return the provider's error code from a refusal, or None.

        The code alone is returned. A provider's error description can echo request values back,
        so it is never read or logged.

        Args:
            response: The refused HTTP response.

        Returns:
            The error code, or None when the reply carried none.

        Example:
            >>> ClientCredentialsTokenProvider._error_code_of(response)  # doctest: +SKIP
            'invalid_client'
        """
        try:  # The refusal is usually JSON carrying a short error code                              # try read
            body = response.json()  # Parse the refusal                                              # parse
        except Exception:  # Not JSON, or not readable                                               # unreadable
            return None  # Nothing to report                                                         # signal none
        code = body.get("error") if isinstance(body, dict) else None  # The short code               # read code
        return str(code) if code else None  # Report the code only                                   # return code
