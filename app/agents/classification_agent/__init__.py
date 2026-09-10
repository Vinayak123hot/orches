####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Package marker for the classification agent. It declares what lives here and where to start.     #
#                                                                                                  #
# Entry point:                                                                                     #
#   main.py -> FirstClassificationAgent.classify(conversation_id, message)                         #
#   Everything else in this folder is reached through that one call.                               #
#                                                                                                  #
# Modules:                                                                                         #
#   main.py                  the entry point and the contract the calling application reads        #
#   turn_orchestrator.py     drives one turn: the agent loop, the candidate feed, the budgets      #
#   foundry_agent_client.py  the instrumented gateway to the Foundry agent                         #
#   servicenow_kb_source.py  runs a search and shapes the results into candidates                  #
#   servicenow_search_client.py  the HTTP client for the knowledge search endpoint                 #
#   servicenow_token_provider.py  obtains and holds the bearer token the search is called with     #
#   service_contracts.py     the validated shapes one turn crosses                                 #
#   runtime_config.py        loads app/config.yaml and the search endpoint's connection details    #
#   model_cost_meter.py      prices response token usage and logs it                               #
#   backoff_retry.py         bounded exponential-backoff retry for remote calls                    #
#   correlation_ids.py       mints a log key when no conversation id is available                  #
#   params.env               template for the search endpoint's connection details                 #
#                                                                                                  #
# Structured logging comes from app.event_hub, through the factory the application injects.        #
#                                                                                                  #
# Nothing is constructed at import. The agent takes its collaborators as constructor arguments,    #
# so a test can drive it with a stub and no network at all.                                        #
####################################################################################################
